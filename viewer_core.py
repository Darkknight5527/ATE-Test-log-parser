"""Data behind the viewer tabs (Detailed Info, Charts, Lot Traveller, Production Lots).

All STDF reading goes through stdf_lot_yield_summary (one read per file, every test value kept) and a cache, so a
file that is already known is not read again. Nothing here writes to the user's data folders.
"""
import datetime
import hashlib
import json
import math
import os
import pickle
import re
import statistics
from collections import Counter, OrderedDict

import stdf_lot_yield_summary as core
import profiles

FLOW = ["Room", "PostBake", "Hot", "Cold"]
AREA_KEYS = {"all": None, "room": "Room", "postbake": "PostBake", "hot": "Hot", "cold": "Cold"}


def lot_from(text):
    """Lot number found in a text with the profile's lot_regex (e.g. LOT000123 -> LOT123), else ''."""
    if core.LOT_RX is None or not text:
        return ""
    m = core.LOT_RX.search(text)
    if not m:
        return ""
    return core.LOT_PREFIX + (m.group(1) if m.groups() else m.group(0))


def is_lot_number(k):
    return bool(k) and core.LOT_RX is not None and lot_from(k) == k


def lot_label(paths, keys):
    """Lot number from the datalogs, else a lot number in the folder name, else the folder name."""
    prod = Counter({k: n for k, n in keys.items() if is_lot_number(k)})
    if prod:
        return prod.most_common(1)[0][0]
    try:
        folder = os.path.commonpath([os.path.dirname(p) for p in paths])
    except ValueError:
        folder = os.path.dirname(paths[0]) if paths else ""
    name = os.path.basename(os.path.normpath(folder))
    if lot_from(name):
        return lot_from(name)
    if name and len(name) < 6:
        name = "%s %s" % (os.path.basename(os.path.dirname(os.path.normpath(folder))), name)
    return name or (keys.most_common(1)[0][0] if keys else "Lot")


def default_output(paths, area, lot):
    folder = os.path.dirname(paths[0]) if paths else os.path.expanduser("~")
    lot = re.sub(r"[^\w\-]+", "_", lot or "Lot")[:40]
    tag = "" if area == "all" else "_" + {"room": "Room", "postbake": "PostBake", "hot": "Hot", "cold": "Cold"}[area]
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    return os.path.join(folder, "%s_Lot_Yield_Report%s_%s.xlsx" % (lot, tag, stamp))


# ------------------------------------------------------------------------------------------------------------
# cache: path + size + modified time -> pickled reading (memory, and on disk in the user's app folder)
# ------------------------------------------------------------------------------------------------------------
def _parse_to_cache(path, fp, prof=None):
    """worker process: read one STDF and store the reading in the cache folder"""
    if prof is not None:
        profiles.apply(prof)                  # worker processes start fresh: give them the active profile
    s = core.read_stdf(path, core.Options(), keep_values=True)
    b = pickle.dumps(s, protocol=pickle.HIGHEST_PROTOCOL)
    tmp = "%s.%d.tmp" % (fp, os.getpid())
    with open(tmp, "wb") as fh:
        fh.write(b)
    os.replace(tmp, fp)
    return True


def report_job(paths, area, output, cache_dir, prof=None, file_sheets=True):
    """worker process: full lot analysis + Excel report. Returns the summary shown on the Results tab."""
    if prof is not None:
        profiles.apply(prof)
    reader = Reader(cache_dir)
    tracker, fin, files, skipped = analyze_paths(paths, reader)
    lot = lot_label(paths, Counter(lot_key(f) for f in files))
    scope = AREA_KEYS[area]
    if scope and not tracker.parts[scope]:
        raise core.AnalysisError("No %s files in this lot." % core.TEMP_LABEL[scope])
    out = output or default_output(paths, area, lot)
    extra = None
    if file_sheets:
        import file_sheets as FS
        extra = lambda wb: FS.add_file_sheets(wb, files, scope=scope, log=lambda *_: None)  # noqa: E731
    core.export_multi_stage_excel(out, tracker, fin, files + skipped, skipped, scope=scope, extra_sheets=extra)
    summ = core.summary_dict(tracker, fin, files, skipped)
    summ.update(output=out, area=area, lot=lot, files_used=len(files))
    return summ


class Reader:
    MEM_LIMIT = 400 * 1024 * 1024        # readings kept in memory (bytes); the rest is re-read from the disk cache
    def __init__(self, cache_dir, opts=None):
        self.dir = cache_dir
        os.makedirs(self.dir, exist_ok=True)
        self.opts = opts                       # None = the active profile's settings at read time
        self.mem = OrderedDict()
        self.mem_bytes = 0
        self.prune()

    def prune(self, days=60):
        """forget readings of files not opened for a while (keeps the cache small)"""
        import time
        limit = time.time() - days * 86400
        try:
            for n in os.listdir(self.dir):
                fp = os.path.join(self.dir, n)
                if n.endswith(".pkl") and os.path.getatime(fp) < limit and os.path.getmtime(fp) < limit:
                    os.remove(fp)
        except OSError:
            pass

    def key(self, path):
        st = os.stat(path)
        return hashlib.sha1(f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}|{core.VERSION}|{profiles.signature()}".encode()).hexdigest()

    def blob(self, path):
        k = self.key(path)
        b = self.mem.get(k)
        if b is None:
            fp = os.path.join(self.dir, k + ".pkl")
            if os.path.exists(fp):
                try:
                    with open(fp, "rb") as fh:
                        b = fh.read()
                    os.utime(fp, None)
                except OSError:
                    b = None
            if b is None:
                s = core.read_stdf(path, self.opts or core.Options(), keep_values=True)
                b = pickle.dumps(s, protocol=pickle.HIGHEST_PROTOCOL)
                try:
                    with open(fp, "wb") as fh:
                        fh.write(b)
                except OSError:
                    pass
            self.mem[k] = b
            self.mem_bytes += len(b)
            while len(self.mem) > 1 and (self.mem_bytes > self.MEM_LIMIT or len(self.mem) > 80):
                self.mem_bytes -= len(self.mem.popitem(last=False)[1])
        return b

    def warm(self, paths, submit):
        """read the files that are not in the cache yet, several at a time in worker processes"""
        todo = []
        for p in dict.fromkeys(paths):
            try:
                k = self.key(p)
            except OSError:
                continue
            fp = os.path.join(self.dir, k + ".pkl")
            if k not in self.mem and not os.path.exists(fp):
                todo.append((os.path.getsize(p), p, fp))
        if len(todo) < 2 or submit is None:
            return 0
        futs = []
        for _, p, fp in sorted(todo, reverse=True):        # biggest first keeps all workers busy
            f = submit(_parse_to_cache, p, fp, profiles.current())
            if f is None:
                return 0                                     # no workers - files are read one by one later
            futs.append(f)
        for f in futs:
            try:
                f.result()
            except Exception:  # noqa - the normal read reports the problem with the file name
                pass
        return len(futs)

    def __call__(self, path):
        """A fresh copy (the lot analysis changes the objects it is given)."""
        return pickle.loads(self.blob(path))

    def clear_disk(self):
        for n in os.listdir(self.dir):
            if n.endswith(".pkl"):
                try:
                    os.remove(os.path.join(self.dir, n))
                except OSError:
                    pass


def fmt_time(t, traveller=False):
    if not t:
        return ""
    d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
    if traveller:   # the way the lot traveller writes it: 11/6/2026 4:32:13 PM
        return f"{d.month}/{d.day}/{d.year} {d.strftime('%I').lstrip('0')}:{d.strftime('%M:%S %p')}"
    return d.strftime("%Y-%m-%d %H:%M:%S")


def lot_key(f):
    return lot_from(f.part_type) or lot_from(f.lot_id) or f.lot_id or "?"


def fuse_key(run_or_unit):
    """Comparable fuse ID of a unit (None when the chip has none)."""
    fp = run_or_unit.fingerprint
    if fp is None:
        return None
    kind, val = fp
    return core.f32(val) if kind == "word" else val


# ------------------------------------------------------------------------------------------------------------
# one loaded file in the viewer
# ------------------------------------------------------------------------------------------------------------
class ViewFile:
    _n = 0

    def __init__(self, path, reader):
        ViewFile._n += 1
        self.id = f"f{ViewFile._n}"
        self.path = path
        self.s = reader(path)                     # read-only copy for the viewer
        self.s.temperature = core.detect_temperature(self.s)
        self.units = core.build_units(self.s, core.Exceptions())
        self.chip = None                          # chip-level numbers from the lot analysis (set later)
        self.stage = ""
        excl = core.SANITY_RX.search(" ".join((self.s.file_name, self.s.lot_id, self.s.job_name)))
        self.note = "Sanity check file - left out of lot counts" if excl else ""

    def row(self):
        s = self.s
        u_pass = sum(1 for u in self.units if u.passed)
        sites = sorted({r.site for r in s.runs})
        c = self.chip
        devices = c["chips"] if c else len(self.units)
        passed = c["pass"] if c else u_pass
        return dict(id=self.id, name=s.file_name, path=self.path, lot=s.lot_id, lot_key=lot_key(s), part_type=s.part_type,
                    job=s.job_name, tester=s.tester or "", node=s.node, sites=len(sites) or 1,
                    area=s.temperature, start=fmt_time(s.start_t), stop=fmt_time(s.finish_t),
                    runs=len(s.runs), devices=devices, retested=sum(1 for u in self.units if len(u.runs) > 1),
                    passed=passed, failed=devices - passed, yield_=(passed / devices) if devices else 0.0,
                    run_pass=sum(1 for r in s.runs if r.hb == 1), tests=len(s.tests), stage=self.stage,
                    chip_level=bool(c), note=self.note, repeats=(c or {}).get("repeats", 0))


# ------------------------------------------------------------------------------------------------------------
# lot analysis of a set of files (chip-level per-file numbers, traveller blocks, production summary)
# ------------------------------------------------------------------------------------------------------------
def analyze_paths(paths, reader, log=lambda *_: None):
    opts = core.Options()
    tracker, fin, files, skipped = core.analyze(list(paths), opts, log=log, reader=reader)
    return tracker, fin, files, skipped


def per_file_chips(tracker, files):
    """For every used file: distinct chips in it, their result in that file, bins (traveller numbers)."""
    out = {}
    by_file = {id(f): {} for f in files}
    part_files = {}
    for temp in tracker.parts:
        for p in tracker.parts[temp]:
            for u in p.units:
                d = by_file.get(id(u.file))
                if d is not None:
                    d.setdefault(p.no, []).append(u)
                    part_files.setdefault(p.no, set()).add(id(u.file))
    for f in files:
        d = by_file[id(f)]
        bins = Counter()
        passed = 0
        for us in d.values():
            last = us[-1].last
            bins[last.hb] += 1
            passed += last.hb == 1
        out[f.file_path] = dict(chips=len(d), pass_=passed, bins=dict(bins),
                                repeats=sum(len(us) - 1 for us in d.values()), parts=set(d))
    # an initial-test file whose chips were (almost) all tested again in a LATER initial-test file of the same
    # area was re-run (e.g. a set-up day): the later file is the one for the traveller
    for f in files:
        if f.stage != "Fresh Run":
            continue
        mine = out[f.file_path]["parts"]
        later = set()
        for g in files:
            if g is not f and g.temperature == f.temperature and g.stage == "Fresh Run" and g.start_t > f.start_t:
                later |= out[g.file_path]["parts"]
        out[f.file_path]["rerun_later"] = bool(mine) and len(mine & later) >= 0.9 * len(mine)
    for v in out.values():
        v.pop("parts")
    return out


def traveller_blocks(tracker, files):
    """Blocks the way the lot traveller is filled: per area -> T1 (initial), T2 (1st retest) ... -> one block per file."""
    chips = per_file_chips(tracker, files)
    areas = []
    for temp in tracker.order:
        tfiles = [f for f in files if f.temperature == temp]
        if not tfiles:
            continue
        sheets = OrderedDict()
        for f in sorted(tfiles, key=lambda x: x.start_t):
            k = "T1 - Initial Test" if f.stage == "Fresh Run" else \
                (f"T{int(f.stage.split()[-1]) + 1} - " + {1: "1st", 2: "2nd", 3: "3rd"}.get(int(f.stage.split()[-1]),
                                                                                       f"{f.stage.split()[-1]}th") + " Re-Test")
            c = chips[f.file_path]
            bins = {str(b): n for b, n in c["bins"].items()}
            sheets.setdefault(k, []).append(dict(
                file=f.file_name, start=fmt_time(f.start_t, True), stop=fmt_time(f.finish_t, True),
                lot_size=c["chips"], passed=c["pass_"], failed=c["chips"] - c["pass_"],
                bins=[c["bins"].get(b, 0) for b in range(1, 10)],
                other={b: n for b, n in c["bins"].items() if not 1 <= b <= 9},
                runs=len(f.runs), repeats=c["repeats"], rerun_later=c.get("rerun_later", False)))
        # sheet totals = distinct chips of that stage with their final result in it (a chip retested in three
        # files of the same retest round counts once) - the same numbers as the lot waterfall
        stage_of = {"T1 - Initial Test": "Fresh Run"}
        tot = []
        for k, blocks in sheets.items():
            sname = stage_of.get(k) or "Retest %d" % (int(k.split()[0][1:]) - 1)
            sfiles = {id(f) for f in tfiles if f.stage == sname}
            last = {}
            for p in tracker.parts[temp]:
                for u in p.units:
                    if id(u.file) in sfiles:
                        prev = last.get(p.no)
                        if prev is None or (u.file.start_t, u.runs[-1].row) >= (prev.file.start_t, prev.runs[-1].row):
                            last[p.no] = u
            bins = Counter(u.last.hb for u in last.values())
            n = len(last)
            ok = bins.get(1, 0)
            tot.append(dict(sheet=k, lot_size=n, passed=ok, failed=n - ok, bins=[bins.get(i, 0) for i in range(1, 10)]))
        areas.append(dict(area=temp, label=core.TEMP_LABEL[temp], sheets=[dict(name=k, blocks=v) for k, v in sheets.items()],
                          totals=tot))
    return areas


def production_summary(tracker, fin, files, skipped):
    """Multi-stage recovery waterfall per area + final lot result (plain numbers)."""
    d = core.summary_dict(tracker, fin, files, skipped)
    rows = []
    for t in d["flow"]:
        prev = None
        for st in [x for x in tracker.stages if x.temperature == t]:
            cy = st.cum_good / st.population if st.population else 0.0
            rows.append(dict(area=t, stage=st.name, tested=st.total_parts, passed=st.pass_parts, failed=st.fail_parts,
                             stage_yield=st.pass_parts / st.total_parts if st.total_parts else 0.0,
                             cum_good=st.cum_good, cum_yield=cy, gain=None if prev is None else cy - prev,
                             runs=st.total_runs, recovered=None if st.is_fresh else st.recovered,
                             files=len(st.files)))
            prev = cy
    d["rows"] = rows
    d["traveller"] = traveller_blocks(tracker, files)
    return d


# ------------------------------------------------------------------------------------------------------------
# test statistics / charts
# ------------------------------------------------------------------------------------------------------------
def test_catalog(vfs):
    cat = OrderedDict()
    for vf in vfs:
        for tn, td in vf.s.tests.items():
            if tn not in cat:
                cat[tn] = dict(num=tn, name=td[0], units=td[1], lo=td[2], hi=td[3])
            else:
                c = cat[tn]
                if c["lo"] is None and td[2] is not None:
                    c["lo"] = td[2]
                if c["hi"] is None and td[3] is not None:
                    c["hi"] = td[3]
    return cat


def stats(vals, lo, hi):
    n = len(vals)
    if not n:
        return dict(n=0)
    mean = sum(vals) / n
    sd = statistics.pstdev(vals) if n > 1 else 0.0
    cp = cpk = None
    if sd > 0:
        if lo is not None and hi is not None:
            cp = (hi - lo) / (6 * sd)
        sides = [x for x in ((hi - mean) / (3 * sd) if hi is not None else None,
                             (mean - lo) / (3 * sd) if lo is not None else None) if x is not None]
        cpk = min(sides) if sides else None
    return dict(n=n, min=min(vals), max=max(vals), mean=mean, sd=sd, cp=cp, cpk=cpk)


def clean(x):
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return x


def test_list(vfs, flt):
    """Tests of the checked files with fail counts and Cp/Cpk (all runs), filtered."""
    cat = test_catalog(vfs)
    q = (flt.get("q") or "").strip().lower()
    pf = flt.get("pf", "any")
    out = []
    vals = {tn: [] for tn in cat}
    fails = Counter()
    for vf in vfs:
        for r in vf.s.runs:
            if r.vals:
                for tn, v in r.vals.items():
                    vals[tn].append(v)
                for tn in r.fails:
                    fails[tn] += 1
    for tn, c in cat.items():
        if q and q not in str(tn) and q not in c["name"].lower():
            continue
        if pf == "fail" and not fails[tn]:
            continue
        if pf == "pass" and fails[tn]:
            continue
        st = stats(vals[tn], c["lo"], c["hi"])
        ok = True
        for k in ("cp", "cpk"):
            op, v = flt.get(k + "_op", ">"), flt.get(k)
            if v not in (None, ""):
                try:
                    v = float(v)
                except ValueError:
                    continue
                x = st.get(k)
                if x is None or not ((x > v) if op == ">" else (x < v)):
                    ok = False
        if not ok:
            continue
        out.append(dict(num=tn, name=c["name"], units=c["units"], fails=fails[tn], n=st.get("n", 0),
                        cp=clean(st.get("cp")), cpk=clean(st.get("cpk"))))
    return out


AREA_ORDER = ["Room", "PostBake", "Hot", "Cold"]


def _r(x):
    x = clean(x)
    return float("%.7g" % x) if isinstance(x, float) else x


def _lim(vf, tn):
    td = vf.s.tests.get(tn)
    return (_r(td[2]), _r(td[3])) if td else (None, None)


def test_detail(vfs, tn):
    """numbers of one test per file - each file judged against ITS OWN limits (cold limits can differ)"""
    cat = test_catalog(vfs).get(tn)
    if not cat:
        return None
    rows = []
    allv = []
    sets = OrderedDict()
    for vf in vfs:
        if tn not in vf.s.tests:
            continue
        lo, hi = _lim(vf, tn)
        v = [r.vals[tn] for r in vf.s.runs if r.vals and tn in r.vals]
        f = sum(1 for r in vf.s.runs if r.fails and tn in r.fails)
        st = stats(v, lo, hi)
        allv += v
        sets.setdefault((lo, hi), []).append(vf.s.temperature)
        rows.append(dict(id=vf.id, file=vf.s.file_name, area=vf.s.temperature, job=vf.s.job_name, fails=f,
                         lo=lo, hi=hi, **{k: clean(x) for k, x in st.items()}))
    same = len(sets) == 1
    lo, hi = next(iter(sets)) if sets else (cat["lo"], cat["hi"])
    tot = stats(allv, lo, hi) if same else dict(stats(allv, None, None), cp=None, cpk=None)
    order = lambda a: AREA_ORDER.index(a) if a in AREA_ORDER else 9          # noqa: E731
    limits = [dict(lo=k[0], hi=k[1], areas=sorted(set(a), key=order)) for k, a in sets.items()]
    limits.sort(key=lambda d: order(d["areas"][0]))
    for r in rows:
        r["differs"] = not same and (r["lo"], r["hi"]) != (limits[0]["lo"], limits[0]["hi"])
    return dict(test=cat, rows=rows, limits=limits, same=same, total={k: clean(x) for k, x in tot.items()},
                total_fails=sum(r["fails"] for r in rows))


def _tight(c, ref):
    """which side of cell c is tighter than the reference limits: '' / 'lo' / 'hi' / 'lohi'"""
    if not c or not ref or "lo" not in c or "lo" not in ref:
        return ""
    lo = c["lo"] is not None and ref["lo"] is not None and c["lo"] > ref["lo"]
    hi = c["hi"] is not None and ref["hi"] is not None and c["hi"] < ref["hi"]
    return ("lo" if lo else "") + ("hi" if hi else "")


def limit_table(vfs, q="", only_diff=False, limit=6000):
    """every test x area: low / high limit as written in the datalogs. 'mixed' when files of one area disagree"""
    areas = []
    per = OrderedDict()          # tn -> dict(name, units, cells{area: set((lo, hi))})
    for vf in vfs:
        a = vf.s.temperature
        if a not in areas:
            areas.append(a)
        for tn, td in vf.s.tests.items():
            d = per.get(tn)
            if d is None:
                d = per[tn] = dict(name=td[0], units=td[1], cells={})
            d["cells"].setdefault(a, set()).add((_r(td[2]), _r(td[3])))
    areas.sort(key=lambda a: AREA_ORDER.index(a) if a in AREA_ORDER else 9)
    ql = (q or "").strip().lower()
    rows, n_diff, n_tight = [], 0, 0
    for tn in sorted(per):
        d = per[tn]
        allsets = set()
        cells = {}
        for a in areas:
            st = d["cells"].get(a)
            if not st:
                cells[a] = None
                continue
            allsets |= st
            if len(st) == 1:
                lo, hi = next(iter(st))
                cells[a] = dict(lo=lo, hi=hi)
            else:
                cells[a] = dict(mixed=[dict(lo=x[0], hi=x[1]) for x in sorted(st, key=str)])
        differs = len(allsets) > 1
        ref = cells.get(areas[0]) if areas else None
        tight = {a: _tight(cells.get(a), ref) for a in areas[1:]}
        tight = {a: t for a, t in tight.items() if t}
        n_diff += differs
        n_tight += bool(tight)
        if only_diff == "tight" and not tight:
            continue
        if only_diff and only_diff != "tight" and not differs:
            continue
        if ql and ql not in str(tn) and ql not in (d["name"] or "").lower():
            continue
        rows.append(dict(num=tn, name=d["name"], units=d["units"], cells=cells, differs=differs, tight=tight))
    return dict(areas=areas, total=len(rows), differ=n_diff, tighter=n_tight, rows=rows[:limit])


def trend(vfs, tn):
    cat = test_catalog(vfs).get(tn)
    series = []
    for vf in vfs:
        pts = []
        for r in vf.s.runs:
            if r.vals and tn in r.vals:
                pts.append(dict(x=r.row, y=clean(r.vals[tn]), pid=r.pid, hb=r.hb, fail=bool(r.fails and tn in r.fails)))
        td = vf.s.tests.get(tn)
        series.append(dict(id=vf.id, file=vf.s.file_name, area=vf.s.temperature, points=pts,
                           start=fmt_time(vf.s.start_t)[5:16], lo=td[2] if td else None, hi=td[3] if td else None))
    return dict(test=cat, series=series)


def unit_value(u, tn):
    for r in reversed(u.runs):
        if r.vals and tn in r.vals:
            return r.vals[tn], r
    return None, None


def pair(vfa, vfb, tn):
    """Same chip in two files (matched by fuse ID): values of one test, and the change B - A."""
    cat = test_catalog([vfa, vfb]).get(tn)

    def index(vf):
        d = {}
        for u in vf.units:
            k = fuse_key(u)
            if k is not None:
                d.setdefault(k, []).append(u)
        return d
    ia, ib = index(vfa), index(vfb)
    pts, shared, no_val = [], 0, 0
    for k, ua in ia.items():
        ub = ib.get(k)
        if not ub:
            continue
        if len(ua) > 1 or len(ub) > 1:
            shared += 1          # several chips with this fuse code: not a unique match
            continue
        va, ra = unit_value(ua[0], tn)
        vb, rb = unit_value(ub[0], tn)
        if va is None or vb is None:
            no_val += 1
            continue
        pts.append(dict(a=clean(va), b=clean(vb), d=clean(vb - va), pid_a=ra.pid, pid_b=rb.pid,
                        hb_a=ua[0].last.hb, hb_b=ub[0].last.hb))
    r = None
    if len(pts) > 2:
        xa = [p["a"] for p in pts]
        xb = [p["b"] for p in pts]
        try:
            r = statistics.correlation(xa, xb)
        except (statistics.StatisticsError, AttributeError):
            r = None
    ds = [p["d"] for p in pts]
    dstat = stats(ds, None, None) if ds else dict(n=0)
    return dict(test=cat, a=dict(id=vfa.id, file=vfa.s.file_name, area=vfa.s.temperature),
                b=dict(id=vfb.id, file=vfb.s.file_name, area=vfb.s.temperature),
                points=pts, r=clean(r), delta={k: clean(x) for k, x in dstat.items()},
                matched=len(pts), shared=shared, missing=no_val,
                only_a=sum(1 for k in ia if k not in ib), only_b=sum(1 for k in ib if k not in ia),
                no_id_a=sum(1 for u in vfa.units if fuse_key(u) is None),
                no_id_b=sum(1 for u in vfb.units if fuse_key(u) is None))


def pair_lot(tracker, sel_a, sel_b, tn):
    """Chip identity from the lot analysis. sel = {"area": "Hot"} or {"file": path}. One point per physical chip:
    its last value of the test inside the selection."""
    def side(sel):
        if sel.get("area"):
            t = sel["area"]
            keep = lambda u: u.file.temperature == t     # noqa: E731
        else:
            t = None
            keep = lambda u: u.file.file_path == sel["file"]   # noqa: E731
        return t, keep

    def chip_units(p):
        """all units of one physical chip (room part + its later parts)"""
        us = list(p.units)
        for q in p.later.values():
            us += q.units
        return us
    base = tracker.parts[tracker.order[0]] if tracker.order else []
    ta, ka = side(sel_a)
    tb, kb = side(sel_b)
    pts = []
    na = nb = 0
    for p in base:
        us = chip_units(p)
        ua = [u for u in us if ka(u)]
        ub = [u for u in us if kb(u)]
        na += bool(ua)
        nb += bool(ub)
        if not ua or not ub:
            continue
        va = vb = ra = rb = None
        for u in reversed(sorted(ua, key=lambda x: (x.file.start_t, x.runs[0].row))):
            va, ra = unit_value(u, tn)
            if va is not None:
                break
        for u in reversed(sorted(ub, key=lambda x: (x.file.start_t, x.runs[0].row))):
            vb, rb = unit_value(u, tn)
            if vb is not None:
                break
        if va is None or vb is None:
            continue
        pts.append(dict(a=clean(va), b=clean(vb), d=clean(vb - va), chip=p.label, pid_a=ra.pid, pid_b=rb.pid,
                        hb_a=ra.hb, hb_b=rb.hb))
    r = None
    if len(pts) > 2:
        try:
            r = statistics.correlation([q["a"] for q in pts], [q["b"] for q in pts])
        except (statistics.StatisticsError, AttributeError):
            r = None
    ds = [q["d"] for q in pts]
    dstat = stats(ds, None, None) if ds else dict(n=0)
    return dict(points=pts, r=clean(r), delta={k: clean(x) for k, x in dstat.items()}, matched=len(pts),
                chips_a=na, chips_b=nb, both=sum(1 for p in base if any(ka(u) for u in chip_units(p)) and
                                                  any(kb(u) for u in chip_units(p))))


# ------------------------------------------------------------------------------------------------------------
# saved production lots (file paths only; files stay where they are)
# ------------------------------------------------------------------------------------------------------------
class LotStore:
    def __init__(self, path):
        self.path = path
        try:
            with open(path, encoding="utf-8") as fh:
                self.lots = json.load(fh)
        except Exception:  # noqa
            self.lots = {}

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.lots, fh, indent=1)
        os.replace(tmp, self.path)

    def put(self, name, paths, folders=()):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        lot = self.lots.get(name) or dict(name=name, paths=[], folders=[], created=now)
        have = set(lot["paths"])

        def sig(p):
            try:
                return os.path.basename(p).lower(), os.path.getsize(p)
            except OSError:
                return os.path.basename(p).lower(), -1
        same = {sig(p) for p in lot["paths"]}
        added = []
        for p in paths:                       # skip a file already in the lot (also a copy of it from another folder)
            if p in have or sig(p) in same:
                continue
            added.append(p)
            same.add(sig(p))
        lot["paths"] += added
        lot["folders"] = sorted(set(lot.get("folders", [])) | set(folders))
        lot["updated"] = now
        self.lots[name] = lot
        self.save()
        return added

    def remove_path(self, name, path):
        lot = self.lots[name]
        lot["paths"] = [p for p in lot["paths"] if p != path]
        lot["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        self.save()

    def delete(self, name):
        self.lots.pop(name, None)
        self.save()

    def new_in_folders(self, name):
        lot = self.lots[name]
        have = set(lot["paths"])
        found = []
        for d in lot.get("folders", []):
            if os.path.isdir(d):
                found += [p for p in core.collect_stdf_files(d) if p not in have]
        return sorted(set(found))
