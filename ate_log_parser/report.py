"""One run: STDF files of a lot -> one report workbook (+ optional traveller copy with an 'STDF Fill-in' sheet).

Workbook sheets
  Summary        one row per STDF: stage, role, lot size, pass/fail, what is in the file, suggested traveller sheet, tester check
  Lot Summary    per device and stage: distinct parts, good parts (min-max) vs pass-runs counting, exception counts
  Exceptions     every unit that needs attention
  Part Journeys  every identified physical part and its results in time order
  Unit Ledger    every test unit and how it was classified
  Files          what was counted, duplicates and unreadable files
  Method         the rules in plain words
  01_..., 02_... one sheet per STDF in the classic datalog-viewer layout
"""
import collections, datetime, glob, os, re
from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter as L

from . import daily_sheets as ds
from .lot_trace import device_of, kind_of, stage_totals, tags_of, temperature_of, trace
from .stdf_reader import add_fingerprint, read_stdf
from .traveller import build_fill_sheet, file_rows

A = Font(name='Arial', size=10); B = Font(name='Arial', size=10, bold=True)
W = Font(name='Arial', size=10, bold=True, color='FFFFFF'); HF = PatternFill('solid', fgColor='104861')
T = Font(name='Arial', size=13, bold=True, color='104861'); N = Font(name='Arial', size=9, italic=True, color='595959')
LINK = Font(name='Arial', size=10, color='0563C1', underline='single')
RED = PatternFill('solid', fgColor='F8CBAD'); AMB = PatternFill('solid', fgColor='FFE699'); GRN = PatternFill('solid', fgColor='C6EFCE')
GRY = PatternFill('solid', fgColor='EDEDED')
WR = Alignment(wrap_text=True, vertical='top'); CE = Alignment(horizontal='center', vertical='center', wrap_text=True)


def _hdr(ws, r, labels, widths=None):
    for c, h in enumerate(labels, 1):
        x = ws.cell(r, c, h); x.font = W; x.fill = HF; x.alignment = CE
    ws.row_dimensions[r].height = 42
    if widths:
        for c, w in enumerate(widths, 1): ws.column_dimensions[L(c)].width = w


def _put(ws, r, c, v, font=A, fmt=None, fill=None, al=None):
    x = ws.cell(r, c, v); x.font = font
    if fmt: x.number_format = fmt
    if fill: x.fill = fill
    if al: x.alignment = al
    return x


def collect(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            for ext in ('*.stdf', '*.std', '*.stdf.gz', '*.std.gz', '*.STDF', '*.STD'):
                out += glob.glob(os.path.join(p, '**', ext), recursive=True)
        else:
            out += glob.glob(p) or [p]
    seen, res = set(), []
    for p in out:
        a = os.path.abspath(p)
        if a not in seen: seen.add(a); res.append(a)
    return res


def short_name(f, idx, stage):
    st = f['mir'].get('start')
    return ('%02d_%s_%s' % (idx, stage.replace(' ', ''), st.strftime('%m%d_%H%M') if st else ''))[:31]


def build(inputs, output=None, profile=None, traveller=None, traveller_out=None, stats='pass', log=print):
    from .profile import load
    profile = profile or load()
    ds.CONTI[0] = profile.get('continuity_suite', 'CONTINUITY')
    files, skipped, hashes = [], [], {}
    paths = collect(inputs)
    for n_, p in enumerate(paths, 1):
        log('Reading %d/%d: %s' % (n_, len(paths), os.path.basename(p)))
        try:
            f = read_stdf(p)
        except Exception as e:
            skipped.append((p, 'not read: %s' % e, '')); continue
        if f['md5'] in hashes:
            skipped.append((p, 'identical copy - skipped', hashes[f['md5']]))
            for g in files:
                if g['md5'] == f['md5']: g.setdefault('other_paths', []).append(p)
            continue
        hashes[f['md5']] = p
        if not f['runs']:
            skipped.append((p, 'no part results in file', '')); continue
        add_fingerprint(f, profile)
        files.append(f)
    if not files:
        raise RuntimeError('No usable STDF files found.' + ''.join('\n  %s: %s' % (os.path.basename(p), w) for p, w, _ in skipped))

    groups = collections.defaultdict(list)
    for f in files: groups[device_of(f, profile)].append(f)
    traced = {}
    for dev, FL in groups.items():
        log('Tracing parts for device %s (%d files)' % (dev, len(FL)))
        FLs, units, role = trace(FL, profile)
        traced[dev] = (FLs, units, role, stage_totals(FLs, units), file_rows(FLs, units, profile))
    has_id = any(f.get('has_id') for f in files)

    wb = Workbook()
    sm = wb.active; sm.title = 'Summary'
    ls = wb.create_sheet('Lot Summary'); ex = wb.create_sheet('Exceptions'); pj = wb.create_sheet('Part Journeys')
    ul = wb.create_sheet('Unit Ledger'); fs = wb.create_sheet('Files'); me = wb.create_sheet('Method')

    # per-file sheets
    order = sorted([(f['mir'].get('start') or datetime.datetime.min, dev, f) for dev, v in traced.items() for f in v[0]], key=lambda x: (x[0], x[2]['path']))
    sheet_of, unit_of_run = {}, {}
    for dev, (FL, units, role, tot, frows) in traced.items():
        for u in units:
            for r in u.run_list: unit_of_run[id(r)] = u
    for i, (_, dev, f) in enumerate(order, 1):
        sheet_of[f['path']] = short_name(f, i, temperature_of(f, profile))
    for i, (_, dev, f) in enumerate(order, 1):
        log('Building sheet %s' % sheet_of[f['path']])
        by_sn = collections.defaultdict(list)
        for r in f['runs']: by_sn[r['sn']].append(r)
        analysis = {}
        for r in f['runs']:
            u = unit_of_run.get(id(r))
            txt = kind_of(u) if u else ''
            if u and u.link: txt += ': ' + u.link
            if u and u.runs > 1: txt = 'retested %dx in this file; ' % u.runs + txt
            analysis[id(r)] = txt
        ds.file_sheet(wb, f, sheet_of[f['path']], dict(by_sn=by_sn, analysis=analysis, temp=temperature_of(f, profile)), stats)

    # ---------------- Summary
    _put(sm, 1, 1, 'STDF Lot Report', T)
    _put(sm, 2, 1, 'Generated %s from %d file(s). Profile: %s.%s' % (
        datetime.datetime.now().strftime('%Y-%m-%d %H:%M'), len(files), profile.get('name'),
        '' if has_id else '  No part-ID test configured/found: parts can only be followed inside a file.'), N)
    cols = ['Sheet', 'File', 'Device', 'Lot (in file)', 'Program', 'Stage', 'Role', 'Start (tester clock)', 'Stop', 'Test runs',
            'Lot size (parts in file)', 'Pass', 'Fail', 'Yield', 'First time at this stage', 'Retests of earlier fails',
            'Passed part tested again', 'Possible repeats', 'Extra parts in retest', 'Tester pass count (HBR)', 'Check vs tester',
            'Suggested traveller sheet']
    _hdr(sm, 4, cols, [24, 50, 8, 34, 22, 12, 8, 16, 16, 7, 9, 7, 7, 8, 9, 9, 9, 9, 9, 9, 10, 18])
    r = 5
    for _, dev, f in order:
        FL, units, role, tot, frows = traced[dev]
        fr = next(x for x in frows if x['fi'] == FL.index(f))
        c = fr['cls']
        good = sum(cn for cn, p, _n in f['hbr'].values() if p == 'P') if f['hbr'] else None
        x = _put(sm, r, 1, sheet_of[f['path']], LINK); x.hyperlink = "#'%s'!A1" % sheet_of[f['path']]
        vals = [os.path.basename(f['path']), dev, f['mir'].get('lot'), f['mir'].get('job'), fr['stage'], role[FL.index(f)],
                f['mir']['start'].strftime('%Y-%m-%d %H:%M') if f['mir'].get('start') else '', f['finish'].strftime('%Y-%m-%d %H:%M') if f.get('finish') else '',
                fr['runs'], fr['units'], fr['passq'], fr['failq'], None, c.get('first time at this stage', 0), c.get('retest of earlier fail', 0),
                c.get('passed part tested again', 0), c.get('possible repeat', 0), c.get('extra part in retest', 0),
                good if good is not None else 'n/a', None, fr['sheet']]
        for ci, v in enumerate(vals, 2): _put(sm, r, ci, v)
        _put(sm, r, 14, '=IF(K%d=0,0,L%d/K%d)' % (r, r, r), fmt='0.0%')
        _put(sm, r, 21, '=IF(NOT(ISNUMBER(T%d)),"no HBR",IF(T%d=L%d,"OK","check"))' % (r, r, r))
        if fr['sheet'].startswith('('):
            for ci in range(2, 23): sm.cell(r, ci).fill = GRY
        r += 1
    end = r - 1
    sm.conditional_formatting.add('U5:U%d' % end, CellIsRule(operator='equal', formula=['"OK"'], fill=GRN))
    sm.conditional_formatting.add('U5:U%d' % end, CellIsRule(operator='equal', formula=['"check"'], fill=AMB))
    sm.conditional_formatting.add('Q5:Q%d' % end, CellIsRule(operator='greaterThan', formula=['0'], fill=RED))
    sm.conditional_formatting.add('R5:S%d' % end, CellIsRule(operator='greaterThan', formula=['0'], fill=AMB))
    sm.freeze_panes = 'C5'
    r = end + 2
    _put(sm, r, 1, 'Check vs tester: the tester pass count (HBR) counts each part once per file, like "Pass" here. "check" usually means a lot split over files '
                   '(the tester summary covers the whole lot) - look before trusting the file.', N)
    if skipped:
        r += 2; _put(sm, r, 1, 'Skipped files', B); r += 1
        for p, why, dup in skipped:
            _put(sm, r, 1, os.path.basename(p)); _put(sm, r, 2, why + (' (same as %s)' % os.path.basename(dup) if dup else ''), N); r += 1

    # ---------------- Lot Summary
    _put(ls, 1, 1, 'Lot Summary - distinct physical parts per stage (duplicates removed)', T)
    _put(ls, 2, 1, '"Pass runs" = every passing unit counted per file (what adding up files gives). "Good parts" = distinct parts whose LAST result at '
                   'that stage is a pass. Min/max differ when parts share an ID word and measurements cannot separate them.', N)
    lcols = ['Device', 'Stage', 'Files', 'Test runs', 'Units (parts per file, summed)', 'Pass runs (adding up files)', 'Distinct parts tested',
             'Extra units (likely re-inserts)', 'Good parts (min)', 'Good parts (max)', 'Yield min', 'Yield max', 'Passes counted twice (max)',
             'CONFIRMED: passed part tested again', 'POSSIBLE repeats', 'Anonymous retests matched to earlier fails',
             'Programmed parts not seen earlier', 'Failed earlier stage, tested at next']
    _hdr(ls, 4, lcols, [10, 14, 7, 8, 10, 10, 10, 10, 9, 9, 8, 8, 10, 11, 10, 12, 11, 11])
    r = 5
    for dev, (FL, units, role, tot, frows) in traced.items():
        for t in tot:
            np_ = t['parts'] - t['extra']
            vals = [dev, t['stage'], t['files'], t['runs'], t['units'], t['pass_runs'], np_, t['extra'], t['good_min'], t['good_max']]
            for ci, v in enumerate(vals, 1): _put(ls, r, ci, v)
            _put(ls, r, 11, '=IF(G%d=0,0,I%d/G%d)' % (r, r, r), fmt='0.0%'); _put(ls, r, 12, '=IF(G%d=0,0,J%d/G%d)' % (r, r, r), fmt='0.0%')
            _put(ls, r, 13, '=F%d-J%d' % (r, r))
            for ci, k in enumerate(['confirmed', 'possible', 'anon_rt', 'unseen', 'check'], 14): _put(ls, r, ci, t[k])
            r += 1
    lend = r - 1
    for colx in ('N', 'R'):
        ls.conditional_formatting.add('%s5:%s%d' % (colx, colx, lend), CellIsRule(operator='greaterThan', formula=['0'], fill=RED))
    for colx in ('M', 'O', 'H', 'Q'):
        ls.conditional_formatting.add('%s5:%s%d' % (colx, colx, lend), CellIsRule(operator='greaterThan', formula=['0'], fill=AMB))
    ls.freeze_panes = 'C5'

    # ---------------- Exceptions
    _put(ex, 1, 1, 'Exceptions - every unit that needs attention', T)
    _hdr(ex, 3, ['Device', 'Stage', 'File start', 'Sheet', 'Part ID', 'Result', 'HB', 'SB', 'Type', 'Explanation', 'Earlier unit it matches', 'Distance'],
         [8, 13, 13, 24, 8, 7, 5, 5, 26, 70, 46, 9])
    r = 4
    kinds = {'CONFIRMED': ('Passed part tested again', RED), 'EXTRA': ('Extra part in retest', AMB), 'CHECK': ('Failed earlier stage, tested at next', RED),
             'programmed on arrival': ('Programmed part not seen earlier', AMB), 'POSSIBLE': ('Possible repeat (check)', AMB)}
    for dev, (FL, units, role, tot, frows) in traced.items():
        for u in units:
            k = next((k for k in kinds if u.cls.startswith(k)), None)
            if not k: continue
            vals = [dev, u.stage, u.when(), sheet_of[u.file['path']], u.pid, u.res, u.hb, u.sb, kinds[k][0], u.cls, u.link,
                    round(u.dist, 3) if u.dist is not None else None]
            for ci, v in enumerate(vals, 1): _put(ex, r, ci, v)
            ex.cell(r, 9).fill = kinds[k][1]; r += 1
    if r == 4: _put(ex, 4, 1, 'No exceptions.', N)
    ex.freeze_panes = 'A4'; ex.auto_filter.ref = 'A3:L%d' % max(r - 1, 3)

    # ---------------- Part journeys
    _put(pj, 1, 1, 'Part journeys - every identified physical part and its results in time order', T)
    _put(pj, 2, 1, 'Part # is an internal number (Part ID restarts in every file). Journey = Stage:Result per test unit.', N)
    _hdr(pj, 4, ['Device', 'Part #', 'Times tested', 'Journey', 'Final Room', 'Final Hot', 'Final Cold', 'Files (time : Part ID)', 'Identified by', 'Flags'],
         [8, 8, 8, 50, 9, 9, 9, 80, 20, 40])
    r = 5
    for dev, (FL, units, role, tot, frows) in traced.items():
        parts = collections.OrderedDict()
        for u in units:
            if u.pp > 0: parts.setdefault(u.pp, []).append(u)
        for pp, U in parts.items():
            fin = {}
            for u in U: fin[u.ins] = u.res
            flags = sorted({k for u in U for k in ('CONFIRMED', 'EXTRA', 'CHECK', 'POSSIBLE') if u.cls.startswith(k)})
            vals = [dev, pp, len(U), ' > '.join('%s:%s' % (u.stage, u.res) for u in U), fin.get('Room'), fin.get('Hot'), fin.get('Cold'),
                    ' | '.join('%s : %s' % (u.when(), u.pid) for u in U), 'ID word' if any(u.word_key for u in U) else 'anonymous (no ID)', ', '.join(flags)]
            for ci, v in enumerate(vals, 1): _put(pj, r, ci, v)
            r += 1
    pend = max(r - 1, 5)
    for colx in ('E', 'F', 'G'):
        pj.conditional_formatting.add('%s5:%s%d' % (colx, colx, pend), CellIsRule(operator='equal', formula=['"P"'], fill=GRN))
        pj.conditional_formatting.add('%s5:%s%d' % (colx, colx, pend), CellIsRule(operator='equal', formula=['"F"'], fill=RED))
    pj.freeze_panes = 'C5'; pj.auto_filter.ref = 'A4:J%d' % pend

    # ---------------- Unit ledger
    _put(ul, 1, 1, 'Unit ledger - every test unit (rows of the same Part ID next to each other merged)', T)
    _hdr(ul, 3, ['Device', 'Stage', 'File start', 'Sheet', 'File role', 'Part ID', 'Rows merged', 'Result', 'HB', 'SB', 'First failing test',
                 'Arrived as', 'ID read-back', 'Part #', 'Classification', 'Linked to'],
         [8, 13, 13, 24, 8, 8, 7, 7, 5, 5, 40, 10, 14, 8, 60, 46])
    r = 4
    for dev, (FL, units, role, tot, frows) in traced.items():
        for u in units:
            rb = u.arr_key if u.arr == 'burned' else ('0' if u.arr == 'unburned' else '')
            arr = {'burned': 'programmed', 'unburned': 'not programmed', 'noread': 'no ID read'}[u.arr]
            vals = [dev, u.stage, u.when(), sheet_of[u.file['path']], role[u.fi], u.pid, u.runs, u.res, u.hb, u.sb, u.first_fail, arr, rb,
                    u.pp if u.pp > 0 else 'anon', u.cls, u.link]
            for ci, v in enumerate(vals, 1): _put(ul, r, ci, v)
            r += 1
    ul.freeze_panes = 'A4'; ul.auto_filter.ref = 'A3:P%d' % max(r - 1, 3)

    # ---------------- Files
    _put(fs, 1, 1, 'Files - what was counted and what was skipped', T)
    _hdr(fs, 3, ['File', 'Folder', 'Counted?', 'Reason / sheet', 'Tags'], [62, 50, 10, 40, 24])
    r = 4
    for _, dev, f in order:
        for ci, v in enumerate([os.path.basename(f['path']), os.path.dirname(f['path']), 'yes', sheet_of[f['path']],
                                ', '.join(tags_of(f, profile))], 1): _put(fs, r, ci, v)
        r += 1
    for p, why, dup in skipped:
        for ci, v in enumerate([os.path.basename(p), os.path.dirname(p), 'no', why + (' (same as %s)' % os.path.basename(dup) if dup else ''), ''], 1):
            _put(fs, r, ci, v, fill=GRY)
        r += 1
    fs.freeze_panes = 'A4'

    # ---------------- Method
    _put(me, 1, 1, 'Method - rules used', T)
    text = [
        ('Files', 'Byte-identical files are counted once. Files are grouped by device (from the program name); devices are never mixed.'),
        ('Units', 'Inside one file, rows of the same Part ID next to each other are one unit (tester retest); its last result counts. '
                  'Part ID means nothing across files (it restarts at 1).'),
        ('ID read-back', 'The ID test (profile) at the start of the flow: non-zero = part already programmed (carries its ID word); '
                         '0 = not yet programmed; missing = failed before the read.'),
        ('Programmed parts', 'Linked to the earlier unit with the same ID word. ID words are not unique (trim codes), so when several earlier '
                             'parts share it the comparison measurements decide (same stage only).'),
        ('Passed part tested again', 'CONFIRMED when the closest earlier same-stage match is a PASS and measurements match closely; POSSIBLE when '
                                     'the word matches an earlier pass but measurements cannot decide (common at Hot/Cold).'),
        ('Anonymous parts', 'Not programmed / no read: cannot be identified one by one. In a retest file they are matched by count to earlier '
                            'fails at that stage (each fail once per file); more than that = EXTRA. In a fresh Room file they are new parts. '
                            'At Hot/Cold they are parts already in the lot.'),
        ('Good parts', 'Every pass is programmed, so passes are always identifiable: double-counted passes are always found. Anonymous parts '
                       'are only ever fails and never change the good count.'),
        ('Stages', 'Room / Hot / Cold from the profile rules on program and file name (+ Post-Bake as its own stage). A file is a retest file if its '
                   'name says so, or if more than 20% of its Room parts arrive already programmed.'),
        ('Distance', 'Median over comparison tests of |difference| / normal part-to-part spread. Thresholds per stage are in the profile.'),
    ]
    for i, (a, b) in enumerate(text):
        _put(me, 3 + i, 1, a, B, al=WR); _put(me, 3 + i, 2, b, A, al=WR)
    me.column_dimensions['A'].width = 24; me.column_dimensions['B'].width = 140

    wb.calculation.fullCalcOnLoad = True
    out = output or os.path.join(os.path.dirname(files[0]['path']), 'STDF_Lot_Report_%s.xlsx' % datetime.datetime.now().strftime('%Y%m%d_%H%M'))
    log('Saving %s ...' % out)
    wb.save(out)
    outputs = [out]

    if traveller:
        dev = max(traced, key=lambda d: len(traced[d][0]))
        if len(traced) > 1: log('Note: several devices found; traveller filled for device %s' % dev)
        FL, units, role, tot, frows = traced[dev]
        tout = traveller_out or os.path.join(os.path.dirname(out), os.path.splitext(os.path.basename(traveller))[0] + '_STDF_fill-in.xlsx')
        lot_name = os.path.splitext(os.path.basename(traveller))[0]
        log('Adding STDF Fill-in sheet to a copy of %s' % os.path.basename(traveller))
        build_fill_sheet(traveller, tout, lot_name, FL, units, tot, profile)
        outputs.append(tout)

    lines = ['Report written: %s' % out] + (['Traveller copy: %s' % outputs[1]] if len(outputs) > 1 else [])
    for dev, (FL, units, role, tot, frows) in traced.items():
        for t in tot:
            lines.append('  %-6s %-14s files %2d  parts %4d  good %s  confirmed repeats %d  possible %d' % (
                dev, t['stage'], t['files'], t['parts'] - t['extra'],
                t['good_max'] if t['good_min'] == t['good_max'] else '%d-%d' % (t['good_min'], t['good_max']), t['confirmed'], t['possible']))
    for p, why, dup in skipped:
        lines.append('  SKIPPED %s: %s' % (os.path.basename(p), why))
    for x in lines: log(x)
    return outputs, lines
