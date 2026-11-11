"""One sheet per STDF file in the classic datalog-viewer layout (header, yield / fail summary, reject pareto, Cpk,
outlier summary, statistics block, FAIL UNITS / PASS UNITS with every test value), added to the lot report.

The 'Retest Analysis' column is filled from the lot analysis of stdf_lot_yield_summary.py, so it says the same
thing as the rest of the report: which chip (R12, H5 ...) a run belongs to, whether it was retested in the file,
and where else that chip was tested.
"""
import collections
import os

import datalog_reader as DR
import datalog_sheets as DS

AREA_SHORT = {"Room": "Room", "PostBake": "PostBake", "Hot": "Hot", "Cold": "Cold"}


def sheet_name(f, idx, used):
    t = f.start_t
    import datetime
    st = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%m%d_%H%M") if t else ""
    name = "%02d_%s_%s" % (idx, AREA_SHORT.get(getattr(f, "temperature", ""), "File"), st)
    name = name[:31]
    base, n = name, 2
    while name in used:
        name = ("%s_%d" % (base[:28], n))[:31]
        n += 1
    used.add(name)
    return name


def _ref(u, here=None):
    res = "PASS" if u.passed else "FAIL"
    if here is not None and u.file is here.file:
        return "PID %s earlier in this file (%s)" % (u.pid, res)
    return "%s PID %s (%s)" % (u.file.file_name, u.pid, res)


def analysis_texts(f):
    """{run row (1-based PRR position in the file): text} from the lot analysis of file f."""
    out = {}
    for u in getattr(f, "units", []):
        p = u.part
        if p is None:
            txt = "not counted"
        else:
            bits = ["chip %s" % p.label]
            if p.room_part is not None:
                bits[0] += " (from %s)" % p.room_part.label
            here = p.units.index(u) if u in p.units else 0
            before = p.units[:here]
            if before:
                prev = before[-1]
                kind = "PASSED chip tested again" if prev.passed else "retest of earlier fail"
                bits.append("%s - also tested in %s" % (kind, _ref(prev, u)))
            if len(u.runs) > 1:
                bits.insert(1, "retested %dx in this file" % len(u.runs))
            if u.note:
                bits.append(u.note)
        for r in u.runs:
            out[r.row] = "; ".join(bits) if p is not None else txt
    return out


def add_file_sheets(wb, files, scope=None, stats_on="pass", log=print):
    """Add one sheet per used file (in time order). files: STDFFileSummary objects of the lot analysis."""
    used_names = set(wb.sheetnames)
    shown = [f for f in sorted(files, key=lambda x: (x.start_t, x.file_name))
             if getattr(f, "status", "Used") == "Used" and (scope is None or getattr(f, "temperature", "") == scope)]
    for i, f in enumerate(shown, 1):
        name = sheet_name(f, i, used_names)
        log("  per-file sheet %s  (%s)" % (name, f.file_name))
        try:
            d = DR.read_stdf(f.file_path)
        except Exception as e:  # noqa - one unreadable file must not stop the report
            ws = wb.create_sheet(name)
            ws.cell(1, 1, "Could not read %s for this sheet: %s" % (f.file_name, e))
            continue
        by_sn = collections.defaultdict(list)
        for r in d["runs"]:
            by_sn[r["sn"]].append(r)
        texts = analysis_texts(f)
        analysis = {id(r): texts.get(k, "") for k, r in enumerate(d["runs"], 1)}
        temp = {"Room": "Room 25C", "PostBake": "Post-Bake 25C", "Hot": "Hot 125C", "Cold": "Cold -55C"}.get(
            getattr(f, "temperature", ""), "")
        DS.file_sheet(wb, d, name, dict(by_sn=by_sn, analysis=analysis, temp=temp), stats_on)
    return len(shown)
