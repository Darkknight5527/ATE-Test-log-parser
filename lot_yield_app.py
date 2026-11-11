"""Lot Yield Viewer - desktop app.

Tabs: Detailed Info (files + tests, Report -> Excel + Results), Charts (Trend / Correlation / Delta),
Lot Traveller (numbers to copy into the traveller), Production Lots (saved lots, add the newest STDF, summary).
A local web page (ui/index.html) in a native window via pywebview (Edge WebView2 on Windows). All counting is done by
stdf_lot_yield_summary.py; viewer_core.py prepares the numbers for the page.

Run:  python lot_yield_app.py          (needs: pip install openpyxl pywebview)
"""
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import traceback
from collections import Counter

FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
CODE = getattr(sys, "_MEIPASS", HERE) if FROZEN else HERE
UI_DIR = os.path.join(CODE, "ui")
for p in (CODE, os.path.dirname(CODE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import stdf_lot_yield_summary as core  # noqa: E402
import profiles  # noqa: E402
import viewer_core as V  # noqa: E402

STDF_TYPES = ("STDF files (*.stdf;*.std;*.stdf.gz;*.std.gz)", "All files (*.*)")
from viewer_core import AREA_KEYS, default_output, lot_label  # noqa: E402,F401

# ------------------------------------------------------------------------------------------------------------
# heavy work (reading new STDF files, writing reports) runs in separate worker processes, so the window itself
# never waits on it and Windows does not mark it "Not Responding". Falls back to this process if workers fail.
# ------------------------------------------------------------------------------------------------------------
_POOL = None
_POOL_LOCK = threading.Lock()


def workers():
    return max(1, min(4, (os.cpu_count() or 2) - 1))


def pool():
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            import concurrent.futures as cf
            import multiprocessing as mp
            _POOL = cf.ProcessPoolExecutor(max_workers=workers(), mp_context=mp.get_context("spawn"))
        return _POOL


def _reset_pool():
    global _POOL
    with _POOL_LOCK:
        if _POOL is not None:
            try:
                _POOL.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa
                pass
        _POOL = None


def run_job(fn, *args):
    """fn(*args) in a worker process; in this process if the workers cannot start or crash"""
    from concurrent.futures.process import BrokenProcessPool
    try:
        fut = pool().submit(fn, *args)
    except Exception:  # noqa - pool could not start
        _reset_pool()
        return fn(*args)
    try:
        return fut.result()
    except BrokenProcessPool:
        _reset_pool()
        return fn(*args)


def submit_job(fn, *args):
    try:
        return pool().submit(fn, *args)
    except Exception:  # noqa
        _reset_pool()
        return None


def app_dir():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    d = os.path.join(base, "lot_yield_app")
    os.makedirs(d, exist_ok=True)
    return d


def state_path():
    return os.path.join(app_dir(), "app_state.json")


def load_state():
    try:
        with open(state_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa
        return {}


def save_state(st):
    try:
        with open(state_path(), "w", encoding="utf-8") as fh:
            json.dump(st, fh, indent=1)
    except Exception:  # noqa
        pass


class Api:
    def __init__(self):
        self.window = None
        self.profile_error = ""
        try:                                   # the device profile chosen last time
            profiles.apply(profiles.by_name(app_dir(), load_state().get("profile", "")))
        except Exception as e:  # noqa - missing or broken profile file: run with the generic one
            profiles.apply({})
            self.profile_error = "Saved profile could not be loaded (%s) - using the generic one." % e
        self.reader = V.Reader(os.path.join(app_dir(), "cache"))
        self.lots = V.LotStore(os.path.join(app_dir(), "lots.json"))
        self.vfs = {}                 # id -> ViewFile (insertion order)
        self.groups = []              # per lot in the viewer: dict(key, ids, analysis or error)
        self._an = {}                 # analysis cache: tuple(paths) -> (tracker, fin, files, skipped)
        self.last = None              # last report analysis (for "export another area")
        self.busy = False
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ helpers
    def _js(self, fn, payload):
        try:
            self.window.evaluate_js("%s(%s)" % (fn, json.dumps(payload, default=str)))
        except Exception:  # noqa
            pass

    def _analysis(self, paths):
        key = tuple(sorted(paths))
        stamp = tuple(self.reader.key(p) for p in key)
        hit = self._an.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
        self.reader.warm(key, submit_job)
        res = V.analyze_paths(list(key), self.reader)
        self._an[key] = (stamp, res)
        if len(self._an) > 12:
            self._an.pop(next(iter(self._an)))
        return res

    def _sel(self, ids):
        ids = ids or list(self.vfs)
        return [self.vfs[i] for i in ids if i in self.vfs]

    def _regroup(self):
        """Group the loaded files per lot + device and analyse each group (chip-level numbers per file)."""
        # lot names in the datalogs are not reliable (different spellings, or none), so the loaded files are
        # treated as ONE lot per device version (profile device_regex) - like the command-line script does
        groups = {}
        for vf in self.vfs.values():
            groups.setdefault(core.device_variant(vf.s), []).append(vf)
        self.groups = []
        for dev, vfs in groups.items():
            keys = Counter(V.lot_key(v.s) for v in vfs if v.s.part_type or v.s.lot_id)
            prods = {k for k in keys if V.is_lot_number(k)}
            lk = lot_label([v.path for v in vfs], keys)
            g = dict(key=lk, device=dev, ids=[v.id for v in vfs], error="", summary=None,
                     warn=("Lot names differ between files (%s) - counted as one lot" % ", ".join(sorted(prods)[:4]))
                     if len(prods) > 1 else "")
            paths = [v.path for v in vfs]
            for v in vfs:
                v.chip, v.stage = None, ""
            try:
                tracker, fin, files, skipped = self._analysis(paths)
                chips = V.per_file_chips(tracker, files)
                stage = {f.file_path: f.stage for f in files}
                for v in vfs:
                    c = chips.get(v.path)
                    if c:
                        v.chip = {"chips": c["chips"], "pass": c["pass_"], "repeats": c["repeats"],
                                  "rerun_later": c.get("rerun_later", False)}
                    v.stage = stage.get(v.path, "")
                    if v.path in {s.file_path for s in skipped}:
                        v.stage = "Left out"
                d = core.summary_dict(tracker, fin, files, skipped)
                g["summary"] = dict(lot_size=d["lot_size"], good=d["good"], scrap=d["scrap"], hold=d["hold"],
                                    yield_=d["yield"], flow=d["flow"], areas=d["areas"], gaps=d["gaps"])
            except core.AnalysisError as e:
                g["error"] = str(e)
            except Exception as e:  # noqa
                g["error"] = "%s: %s" % (type(e).__name__, e)
            self.groups.append(g)

    def _group_of(self, ids):
        ids = set(ids or self.vfs)
        for g in self.groups:
            if ids & set(g["ids"]):
                return g
        return None

    # ------------------------------------------------------------------ info / dialogs
    def app_info(self):
        st = load_state()
        return dict(version=core.VERSION, recent=st.get("recent", [])[:20], theme=st.get("theme", "dark"),
                    exclude=st.get("exclude", core.DEFAULT_EXCLUDE), profile=profiles.current()["name"],
                    file_sheets=st.get("file_sheets", True),
                    profiles=list(profiles.list_profiles(app_dir())), profile_error=self.profile_error)

    # ------------------------------------------------------------------ device profiles
    def profile_details(self):
        p = profiles.current()
        return dict(p, has_fuse_id=bool(p["readback_test"] or p["fuse_bits_prefix"]), folder=profiles.folder(app_dir()))

    def _profile_changed(self):
        """Readings depend on the profile (fuse word, matching tests): re-read and re-analyse what is loaded."""
        with self.lock:
            self._an.clear()
            self.last = None
            paths = [v.path for v in self.vfs.values()]
            self.vfs.clear()
        if paths:
            self.open_paths(paths)
        return self.state()

    def profile_select(self, name):
        try:
            p = profiles.by_name(app_dir(), name)
        except Exception as e:  # noqa
            return dict(ok=False, error="Could not load the profile: %s" % e)
        profiles.apply(p)
        st = load_state()
        st["profile"] = p["name"]
        save_state(st)
        self.profile_error = ""
        return dict(ok=True, profile=p["name"], state=self._profile_changed())

    def profile_import(self):
        r = self._dialog("open", ("Profile (*.json)", "All files (*.*)"))
        if not r:
            return dict(ok=False)
        try:
            name = profiles.install(app_dir(), r[0])
        except Exception as e:  # noqa
            return dict(ok=False, error="Not a valid profile: %s" % e)
        res = self.profile_select(name)
        res["profiles"] = list(profiles.list_profiles(app_dir()))
        return res

    def set_option(self, key, value):
        if key not in ("file_sheets",):
            return False
        st = load_state()
        st[key] = value
        save_state(st)
        return True

    def set_theme(self, theme):
        st = load_state()
        st["theme"] = theme
        save_state(st)
        return True

    def _dialog(self, kind, types=(), save_name=None, multiple=False):
        import webview
        FD = getattr(webview, "FileDialog", None)       # pywebview 6; the old constant names are deprecated
        k = ({"open": FD.OPEN, "folder": FD.FOLDER, "save": FD.SAVE} if FD else
             {"open": webview.OPEN_DIALOG, "folder": webview.FOLDER_DIALOG, "save": webview.SAVE_DIALOG})[kind]
        kw = dict(allow_multiple=multiple, file_types=types)
        if save_name:
            kw = dict(save_filename=save_name, file_types=types)
        res = self.window.create_file_dialog(k, **kw)
        if not res:
            return []
        return [res] if isinstance(res, str) else list(res)

    def pick_files(self):
        return self._dialog("open", STDF_TYPES, multiple=True)

    def pick_folder(self):
        return self._dialog("folder")

    def pick_output(self):
        r = self._dialog("save", ("Excel workbook (*.xlsx)",), save_name="Lot_Yield_Report.xlsx")
        return r[0] if r else ""

    # ------------------------------------------------------------------ files in the viewer
    def open_paths(self, paths):
        with self.lock:
            have = {v.path for v in self.vfs.values()}
            added, errors = 0, []
            found = []
            for p in paths or []:
                found += [fp for fp in core.collect_stdf_files(p) if fp not in have]
            self.reader.warm(found, submit_job)          # new files are read in parallel, outside the window
            for fp in found:
                if fp in have:
                    continue
                try:
                    vf = V.ViewFile(fp, self.reader)
                except Exception as e:  # noqa
                    errors.append("%s: %s" % (os.path.basename(fp), e))
                    continue
                self.vfs[vf.id] = vf
                have.add(fp)
                added += 1
            items = sorted(self.vfs.values(), key=lambda v: (v.s.start_t, v.s.file_name))
            self.vfs = {v.id: v for v in items}
            self._regroup()
            out = self.state()
            out.update(added=added, errors=errors)
            return out

    def remove_files(self, ids):
        with self.lock:
            for i in ids or []:
                self.vfs.pop(i, None)
            self._regroup()
            return self.state()

    def clear_files(self):
        with self.lock:
            self.vfs.clear()
            self.groups = []
            return self.state()

    def state(self):
        return dict(rows=[v.row() for v in self.vfs.values()],
                    groups=[{k: g[k] for k in ("key", "device", "ids", "error", "summary", "warn")} for g in self.groups])

    # ------------------------------------------------------------------ tests / charts
    def tests(self, ids, flt):
        out = V.test_list(self._sel(ids), flt or {})
        return dict(total=len(out), tests=out[:4000])

    def test_detail(self, ids, tn):
        return V.test_detail(self._sel(ids), int(tn))

    def limits(self, ids, q="", only_diff=False):
        return V.limit_table(self._sel(ids), q, only_diff or False)

    def trend(self, ids, tn):
        return V.trend(self._sel(ids), int(tn))

    def pair(self, sel_a, sel_b, tn):
        """sel = {"area": "Hot"} or {"file": id}. Chip identity from the lot analysis when the files are one lot."""
        tn = int(tn)
        ids = []
        for sel in (sel_a, sel_b):
            if sel.get("file"):
                ids.append(sel["file"])
        g = None
        if sel_a.get("area") or sel_b.get("area"):
            g = self.groups[0] if len(self.groups) == 1 else None
            for gg in self.groups:
                if ids and set(ids) <= set(gg["ids"]):
                    g = gg
        elif ids:
            g = self._group_of(ids)
            if g and not set(ids) <= set(g["ids"]):
                g = None
        cat = V.test_catalog(self._sel(g["ids"] if g else ids)).get(tn)
        if g and not g["error"]:
            tracker = self._analysis([self.vfs[i].path for i in g["ids"]])[0]

            def conv(sel):
                if sel.get("area"):
                    return {"area": sel["area"]}, V.core.TEMP_LABEL[sel["area"]] + " (all files)"
                vf = self.vfs[sel["file"]]
                return {"file": vf.path}, vf.s.file_name
            sa, la = conv(sel_a)
            sb, lb = conv(sel_b)
            res = V.pair_lot(tracker, sa, sb, tn)
            res.update(test=cat, label_a=la, label_b=lb, how="chip identity from the lot analysis",
                       lim_a=self._limits(sel_a, tn), lim_b=self._limits(sel_b, tn))
            return res
        if sel_a.get("file") and sel_b.get("file"):
            res = V.pair(self.vfs[sel_a["file"]], self.vfs[sel_b["file"]], tn)
            res.update(label_a=res["a"]["file"], label_b=res["b"]["file"], how="fuse ID",
                       lim_a=self._limits(sel_a, tn), lim_b=self._limits(sel_b, tn))
            return res
        return dict(error="Pick two files, or load the files of one lot to compare areas.")

    def _limits(self, sel, tn):
        vs = [self.vfs[sel["file"]]] if sel.get("file") else [v for v in self.vfs.values() if v.s.temperature == sel.get("area")]
        for v in vs:
            td = v.s.tests.get(tn)
            if td:
                return [td[2], td[3]]
        return [None, None]

    # ------------------------------------------------------------------ lot traveller
    def traveller(self, ids):
        sel = self._sel(ids)
        groups = {}
        for v in sel:
            groups.setdefault(core.device_variant(v.s), []).append(v)
        out = []
        for dev, vs in groups.items():
            keys = Counter(V.lot_key(v.s) for v in vs if v.s.part_type or v.s.lot_id)
            paths = [v.path for v in vs]
            lk = lot_label(paths, keys)
            try:
                tracker, fin, files, skipped = self._analysis(paths)
                out.append(dict(lot=lk, device=dev, areas=V.traveller_blocks(tracker, files),
                                left_out=[dict(file=s.file_name, why=s.status.replace("Left out: ", "")) for s in skipped]))
            except Exception as e:  # noqa
                out.append(dict(lot=lk, device=dev, error=str(e)))
        return out

    # ------------------------------------------------------------------ report (Excel + Results tab)
    def generate(self, params):
        if self.busy:
            return dict(ok=False, error="A report is already running.")
        paths = params.get("paths")
        if not paths:
            sel = self._sel(params.get("ids"))
            devs = {core.device_variant(v.s) for v in sel if not v.note} - {"?"}
            if len(devs) > 1:
                return dict(ok=False, error="The checked files are from different devices (%s). Check one device only." % ", ".join(sorted(devs)))
            paths = [v.path for v in sel]
        if not paths:
            return dict(ok=False, error="Open STDF files first.")
        self.busy = True
        threading.Thread(target=self._run, args=(dict(params, paths=paths),), daemon=True).start()
        return dict(ok=True)

    def _run(self, params):
        t0 = datetime.datetime.now()
        log = lambda s: self._js("window.onLog", s)   # noqa: E731
        try:
            paths = params["paths"]
            area = params.get("area", "all")
            log("STDF Multi-Stage Lot Yield Analyzer v%s - %d file(s)" % (core.VERSION, len(paths)))
            self.reader.warm(paths, submit_job)
            fs = params.get("file_sheets", load_state().get("file_sheets", True))
            if fs:
                log("Per-file datalog sheets: on (one sheet per file)")
            summ = run_job(V.report_job, paths, area, params.get("output") or None, self.reader.dir, profiles.current(), fs)
            self.last = paths
            out = summ["output"]
            log("Report written: %s" % os.path.basename(out))
            log("Done in %.1f s" % (datetime.datetime.now() - t0).total_seconds())
            st = load_state()
            rec = dict(when=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), lot=summ["lot"], area=area, output=out,
                       files=summ.get("files_used", 0), good=summ["good"], lot_size=summ["lot_size"])
            st["recent"] = [rec] + [r for r in st.get("recent", []) if r.get("output") != out][:19]
            save_state(st)
            self._js("window.onDone", summ)
        except PermissionError:
            self._js("window.onError", "Cannot write the report - is an older copy still open in WPS / Excel? Close it.")
        except core.AnalysisError as e:
            self._js("window.onError", str(e))
        except Exception as e:  # noqa
            self._js("window.onError", "%s: %s" % (type(e).__name__, e))
        finally:
            self.busy = False

    def export_area(self, area):
        if not self.last:
            return dict(ok=False, error="Generate a report first.")
        try:
            return dict(ok=True, output=run_job(V.report_job, self.last, area, None, self.reader.dir, profiles.current(),
                                                 load_state().get("file_sheets", True))["output"])
        except PermissionError:
            return dict(ok=False, error="Cannot write the report - close the open copy in WPS / Excel.")
        except Exception as e:  # noqa
            return dict(ok=False, error=str(e))

    # ------------------------------------------------------------------ production lots
    def lots_list(self):
        out = []
        for name, lot in sorted(self.lots.lots.items()):
            missing = sum(1 for p in lot["paths"] if not os.path.exists(p))
            out.append(dict(name=name, files=len(lot["paths"]), missing=missing, updated=lot.get("updated", ""),
                            folders=lot.get("folders", []), last=lot.get("last")))
        return out

    def lot_create(self, name, paths):
        name = (name or "").strip()
        files = []
        folders = []
        for p in paths or []:
            if os.path.isdir(p):
                folders.append(p)
            files += core.collect_stdf_files(p)
        if not files:
            return dict(ok=False, error="No STDF files found there.")
        if not name:
            # lot number from the datalogs, else a lot number in the folder name, else the folder name
            keys = Counter()
            for f in files[:60]:
                try:
                    k = V.lot_key(self.reader(f))
                except Exception:  # noqa
                    continue
                if V.is_lot_number(k):
                    keys[k] += 1
            folder = os.path.basename(os.path.normpath(folders[0] if folders else os.path.dirname(files[0])))
            if keys:
                name = keys.most_common(1)[0][0]
            elif V.lot_from(folder):
                name = V.lot_from(folder)
            else:
                name = folder or "Lot"
                if len(folder) < 6:      # e.g. "20V" -> "<parent folder> 20V"
                    parent = os.path.basename(os.path.dirname(os.path.normpath(folders[0] if folders else os.path.dirname(files[0]))))
                    name = ("%s %s" % (parent, folder)).strip()
        base_name, n = name, 2
        while name in self.lots.lots and set(self.lots.lots[name]["paths"]) != set(files):
            name = "%s (%d)" % (base_name, n)
            n += 1
        self.lots.put(name, files, folders)
        return dict(ok=True, name=name)

    def lot_add(self, name, paths):
        files = []
        for p in paths or []:
            files += core.collect_stdf_files(p)
        added = self.lots.put(name, files)
        return dict(ok=True, added=[os.path.basename(p) for p in added])

    def lot_check_folder(self, name):
        new = self.lots.new_in_folders(name)
        added = self.lots.put(name, new) if new else []
        return dict(ok=True, added=[os.path.basename(p) for p in added])

    def lot_remove_file(self, name, path):
        self.lots.remove_path(name, path)
        return True

    def lot_rename(self, old, new):
        new = (new or "").strip()
        if not new or new == old:
            return dict(ok=False, error="Give a new name.")
        if new in self.lots.lots:
            return dict(ok=False, error="A lot with that name already exists.")
        self.lots.lots[new] = self.lots.lots.pop(old)
        self.lots.lots[new]["name"] = new
        self.lots.save()
        return dict(ok=True, name=new)

    def lot_delete(self, name):
        self.lots.delete(name)
        return True

    def lot_summary(self, name):
        lot = self.lots.lots.get(name)
        if not lot:
            return dict(error="Lot not found.")
        paths = [p for p in lot["paths"] if os.path.exists(p)]
        missing = [p for p in lot["paths"] if not os.path.exists(p)]
        if not paths:
            return dict(error="None of this lot's files can be found.", missing=missing)
        try:
            tracker, fin, files, skipped = self._analysis(paths)
        except core.AnalysisError as e:
            return dict(error=str(e))
        d = V.production_summary(tracker, fin, files, skipped)
        d.update(name=name, missing=missing, updated=lot.get("updated", ""), folders=lot.get("folders", []))
        d["file_list"] = [dict(name=f.file_name, path=f.file_path, area=getattr(f, "temperature", ""),
                               stage=f.stage if f.status == "Used" else "", used=f.status == "Used",
                               status=f.status, start=f.start_str) for f in sorted(files + skipped, key=lambda x: x.start_t)]
        lot["last"] = dict(lot_size=d["lot_size"], good=d["good"], yield_=d["yield"], files=len(files))
        self.lots.save()
        return d

    def lot_report(self, name, area):
        lot = self.lots.lots.get(name)
        if not lot:
            return dict(ok=False, error="Lot not found.")
        paths = [p for p in lot["paths"] if os.path.exists(p)]
        return self.generate(dict(paths=paths, area=area))

    def lot_paths(self, name):
        lot = self.lots.lots.get(name)
        return [p for p in (lot or {}).get("paths", []) if os.path.exists(p)]

    # ------------------------------------------------------------------ misc
    def open_path(self, path):
        if not path or not os.path.exists(path):
            return False
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True

    def open_folder(self, path):
        return self.open_path(os.path.dirname(path) if os.path.isfile(path) else path)

    def clear_cache(self):
        self.reader.clear_disk()
        self.reader.mem.clear()
        self._an.clear()
        return True


def main():
    try:
        import webview
    except Exception:  # noqa
        print("This app needs pywebview:  python -m pip install pywebview openpyxl")
        print("Without it you can still use the command line:  python stdf_lot_yield_summary.py -i <folder> -o report.xlsx")
        input("Press Enter to close")
        return
    api = Api()
    st = load_state()
    win = webview.create_window("Lot Yield Viewer %s" % core.VERSION, url=os.path.join(UI_DIR, "index.html"),
                                js_api=api, width=1440, height=900, min_size=(1100, 700),
                                background_color="#f3f3f6" if st.get("theme") == "light" else "#0b0b0d")
    api.window = win

    def on_drop(e):
        try:
            files = e["dataTransfer"]["files"]
            paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
            if paths:
                api._js("window.onDropPaths", paths)
        except Exception:  # noqa
            pass

    def bind(window):
        try:
            from webview.dom import DOMEventHandler
            window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)
        except Exception:  # noqa
            pass
    sys.setswitchinterval(0.002)          # the window thread gets its turn quickly while Python works
    threading.Thread(target=pool, daemon=True).start()     # start the workers while the window opens
    try:
        webview.start(bind, win, private_mode=False)
    except Exception:  # noqa
        traceback.print_exc()
        input("Press Enter to close")
    finally:
        _reset_pool()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()      # needed by the .exe for its worker processes
    main()
