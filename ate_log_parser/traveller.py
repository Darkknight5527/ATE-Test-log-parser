"""Lot traveller support: work out what goes into every test-sheet block and add an 'STDF Fill-in' sheet
to a copy of the traveller (the traveller itself is never re-saved)."""
import collections, datetime, os, re
from .lot_trace import kind_of
from .inject import add_sheet

ORDER_STAGES = ['Room', 'Hot', 'Cold']


def read_blocks(path, profile):
    """Blocks already filled in the traveller: one per 'Lot size' row in the test sheets."""
    import openpyxl
    names = [s for v in profile['traveller_sheets'].values() for s in v]
    wb = openpyxl.load_workbook(path, data_only=True)
    res = []
    for name in wb.sheetnames:
        if name not in names: continue
        ws = wb[name]
        for r in range(7, ws.max_row + 1):
            if ws.cell(r, 2).value == 'Lot size':
                res.append(dict(sheet=name, header=ws.cell(r - 6, 2).value, start=ws.cell(r - 5, 4).value, stop=ws.cell(r - 4, 4).value,
                                lot=ws.cell(r, 4).value, passq=ws.cell(r + 2, 4).value,
                                bins=[ws.cell(r + 6, c).value or 0 for c in range(4, 13)]))
    return [b for b in res if b['lot'] or b['start']]


def tfmt(d):
    if d is None: return ''
    return '%d/%d/%d %d:%02d:%02d %s' % (d.month, d.day, d.year, (d.hour % 12) or 12, d.minute, d.second, 'AM' if d.hour < 12 else 'PM')


def tparse(s):
    if isinstance(s, datetime.datetime): return s
    try:
        return datetime.datetime.strptime(' '.join(str(s).split()), '%m/%d/%Y %I:%M:%S %p')
    except Exception:
        return None


def file_rows(FL, units, profile):
    """One row per file: suggested traveller sheet, values for the block, what is in the file."""
    sheets = profile['traveller_sheets']
    rows, rt_count = [], collections.Counter()
    for fi, f in enumerate(FL):
        U = [u for u in units if u.fi == fi]
        if not U: continue
        ins = U[0].ins; stage = U[0].stage
        cls = collections.Counter(kind_of(u) for u in U)
        names = sheets.get(ins) or ['%s_T1' % ins.upper(), '%s_T2' % ins.upper()]
        notes = []
        is_rt = cls['retest of earlier fail'] > len(U) / 2
        excluded = [t for t in U[0].tags if t not in ('RETEST-name', 'POST-BAKE')]
        if excluded:
            sheet = '(leave out - %s)' % '/'.join(excluded); notes.append('Set-up / special file (%s), not a lot test' % ', '.join(excluded))
        elif not is_rt:
            sheet = names[0]
        else:
            rt_count[stage] += 1; k = rt_count[stage]
            sheet = names[min(k, len(names) - 1)] if len(names) > 1 else names[0]
            if k > len(names) - 1: notes.append('retest file no. %d at %s - traveller has %d retest sheet(s)' % (k, ins, len(names) - 1))
        for u in U:
            if u.cls.startswith('CONFIRMED') and u.link_unit is not None:
                pr = u.link_unit
                where = 'in this same file' if pr.fi == u.fi else 'in the %s file' % pr.when()
                notes.append('Part ID %s is the same part as Part ID %s %s, which had already passed (%s on this test)' % (
                    u.pid, pr.pid, where, 'passed again' if u.res == 'P' else 'FAILED'))
        poss = [dict(pid=u.pid, res=u.res, match=u.link.replace('possible match: ', '').split(' | ')[0], dist=u.dist)
                for u in U if u.cls.startswith('POSSIBLE')]
        bins = collections.Counter(u.hb for u in U)
        rows.append(dict(fi=fi, file=os.path.basename(f['path']), stage=stage, ins=ins, start=f['mir'].get('start'), stop=f.get('finish'),
                         runs=len(f['runs']), units=len(U), passq=sum(u.res == 'P' for u in U), failq=sum(u.res == 'F' for u in U),
                         bins=[bins.get(b, 0) for b in range(1, 10)], other_bins={b: n for b, n in bins.items() if b > 9 or b == 0},
                         cls=dict(cls), sheet=sheet, notes=notes, poss=poss))
    # a 1-part file whose part is confirmed again at the start of the next file = set-up part
    for i, x in enumerate(rows):
        if x['units'] == 1 and not x['sheet'].startswith('('):
            nxt = [u for u in units if u.fi == x['fi'] + 1 and u.cls.startswith('CONFIRMED') and u.link_unit is not None and u.link_unit.fi == x['fi']]
            if nxt:
                x['sheet'] = '(leave out - same part retested in next file)'
    return rows


def sheet_order(profile):
    return [s for st in ORDER_STAGES for s in profile['traveller_sheets'].get(st, [])]


def build_fill_sheet(traveller_path, out_path, lot_name, FL, units, totals, profile):
    rows = file_rows(FL, units, profile)
    tb = read_blocks(traveller_path, profile)
    ORDER = sheet_order(profile)
    rows_sorted = sorted(rows, key=lambda x: (ORDER.index(x['sheet']) if x['sheet'] in ORDER else 99, x['start'] or datetime.datetime.min))
    C = {}

    def put(r, c, v, s='n_none'): C[(r, c)] = (v, s)
    put(1, 1, 'STDF Fill-in Data  -  %s' % lot_name, 't_none')
    put(1, 6, 'Light-blue first column = traveller sheet to fill. Grey rows = files to leave out. Red notes = a part that had already passed was tested again.', 'i_none')
    put(2, 1, 'Values read from %d STDF files (tester clock). Lot size = parts in the file with in-file retests merged (same rule as the traveller). '
              'Room temperature / humidity / alarm, Storage ID and operator are not in the STDF - fill those by hand.' % len(rows), 'i_none')
    r = 4
    put(r, 1, 'A.  Values for each test-sheet block', 's_none'); r += 1
    hd = ['Traveller sheet', 'Block', 'Testdata file', 'Start Date, Time', 'Stop Date, Time', 'Lot size', 'Pass', 'Fail', 'Yield',
          'Bin 1', 'Bin 2', 'Bin 3', 'Bin 4', 'Bin 5', 'Bin 6', 'Bin 7', 'Bin 8', 'Bin 9', 'Test runs in file',
          'In your traveller now', 'Check', 'What is in this file', 'Notes']
    for c, h in enumerate(hd, 1): put(r, c, h, 'hdrcell')
    hdr_row = r; heights = {hdr_row: 30}; r += 1
    blockno = collections.Counter(); used = set()
    for x in rows_sorted:
        leave = x['sheet'].startswith('(')
        if not leave: blockno[x['sheet']] += 1
        m = [b for b in tb if tparse(b['start']) and x['start'] and abs((tparse(b['start']) - x['start']).total_seconds()) < 90]
        if m:
            b = m[0]; used.add(id(b))
            diffs = []
            if b['sheet'] != x['sheet']: diffs.append('sheet %s' % b['sheet'])
            if (b['lot'] or 0) != x['units']: diffs.append('lot size %s' % b['lot'])
            if [v or 0 for v in b['bins']] != x['bins']: diffs.append('bins %s' % b['bins'])
            now = '%s block (lot %s, pass %s)' % (b['sheet'], b['lot'], b['passq'])
            chk, chk_s = ('OK', 'wrap_grn') if not diffs else ('different: ' + ', '.join(diffs), 'wrap_amb')
        else:
            now = 'not in traveller'
            chk, chk_s = ('OK - left out', 'wrap_grn') if leave else ('missing in traveller', 'wrap_amb')
        sty = 'n_gry' if leave else 'n_none'
        vals = [x['sheet'], '' if leave else blockno[x['sheet']], x['file'], tfmt(x['start']), tfmt(x['stop']), x['units'], x['passq'], x['failq']]
        for c, v in enumerate(vals, 1): put(r, c, v, ('b_gry' if leave else 'b_blu') if c == 1 else sty)
        put(r, 9, round(x['passq'] / x['units'], 4) if x['units'] else '', 'pct')
        for i in range(9): put(r, 10 + i, x['bins'][i] or None, sty)
        put(r, 19, x['runs'], sty); put(r, 20, now, 'wrap'); put(r, 21, chk, chk_s)
        put(r, 22, '; '.join('%d %s' % (n, k) for k, n in sorted(x['cls'].items(), key=lambda kv: -kv[1])), 'wrap')
        nt = list(x['notes'])
        if x['other_bins']: nt.append('other hard bins: %s' % x['other_bins'])
        if x['poss']: nt.append('%d possible repeat(s) - see section C' % len(x['poss']))
        put(r, 23, '\n'.join(nt), 'wrap_red' if any('same part as' in n for n in nt) else 'wrap')
        r += 1
    for b in tb:
        if id(b) not in used:
            put(r, 1, b['sheet'], 'b_amb'); put(r, 3, b['header'] or '', 'wrap'); put(r, 4, str(b['start']))
            put(r, 21, 'traveller block with no matching STDF file', 'wrap_amb'); r += 1
    # ---- B
    r += 2
    put(r, 1, 'B.  Lot totals with duplicates removed (compare with the Overview sheet)', 's_none'); r += 1
    put(r, 1, 'The Overview adds up every block, so a part tested in two files is counted twice. These are distinct physical parts.', 'i_none'); r += 1
    hb = {1: 'Stage', 2: '', 3: 'Comment', 4: 'Overview-style lot size (sum of initial-test blocks)', 5: 'Overview-style passed (sum of all blocks)',
          6: 'Distinct parts', 7: 'Good (min)', 8: 'Good (max)', 9: 'Yield min', 10: 'Yield max', 11: 'Passes counted twice'}
    for c, h in hb.items(): put(r, c, h, 'hdrcell')
    heights[r] = 54; r += 1
    for t in totals:
        sel = [x for x in rows if x['stage'] == t['stage'] and not x['sheet'].startswith('(')]
        first = profile['traveller_sheets'].get(t['stage'].split()[0], [''])[0]
        lot_size = sum(x['units'] for x in sel if x['sheet'] == first)
        passed = sum(x['passq'] for x in sel)
        np_ = t['parts'] - t['extra']
        put(r, 1, t['stage'], 'b_blu')
        for c, v in ((4, lot_size), (5, passed), (6, np_), (7, t['good_min']), (8, t['good_max']), (11, passed - t['good_max'])): put(r, c, v)
        if np_:
            put(r, 9, round(t['good_min'] / np_, 4), 'pct'); put(r, 10, round(t['good_max'] / np_, 4), 'pct')
        com = []
        if t['good_min'] != t['good_max']:
            com.append('Range: some parts share an ID word with a part that already passed here, and measurements cannot tell them apart (section C).')
        if t['extra']:
            com.append('%d extra unit(s) in a retest file beyond the earlier fails - most likely the same part re-inserted under a new Part ID '
                       '(counted as re-inserts; if they were new parts, parts = %d).' % (t['extra'], t['parts']))
        if lot_size > np_: com.append('Initial-test lot sizes add up to %d, but only %d distinct parts were tested.' % (lot_size, np_))
        if passed > t['good_max']: com.append('%d pass(es) counted twice by adding up blocks.' % (passed - t['good_max']))
        put(r, 3, ' '.join(com) or 'No duplicates found.', 'wrap'); r += 1
    # ---- C
    r += 2
    put(r, 1, 'C.  Possible repeats - same ID word as a part that already passed at this stage; measurements cannot decide', 's_none'); r += 1
    put(r, 1, 'Each one is either a new part that happens to share the ID word, or the same part tested again. The tray / handler log for these Part IDs settles it.', 'i_none'); r += 1
    for c, h in {1: 'Traveller sheet', 2: 'Part ID', 3: 'Possible earlier part', 4: 'File start', 5: 'Result', 6: 'Distance'}.items(): put(r, c, h, 'hdrcell')
    r += 1
    anyp = False
    for x in rows_sorted:
        for p in x['poss']:
            anyp = True
            put(r, 1, x['sheet']); put(r, 2, p['pid']); put(r, 3, p['match']); put(r, 4, tfmt(x['start'])); put(r, 5, p['res'])
            put(r, 6, round(p['dist'], 3) if p['dist'] is not None else ''); r += 1
    if not anyp: put(r, 1, 'None.', 'i_none'); r += 1
    # ---- D
    r += 2
    put(r, 1, 'D.  How these values were made', 's_none'); r += 1
    for t in ['Start / Stop = start and finish records of the STDF.',
              'Lot size, Pass, Fail, Bins = one count per part per file: rows of the same Part ID next to each other are one part (last result counts).',
              'Sheet suggestion: most parts tested for the first time at that temperature -> initial-test sheet; most parts are retests of earlier fails '
              '-> retest sheet (in time order). Set-up files and a 1-part file whose part is retested in the next file are left out.',
              'Duplicates: parts are linked between files by the programmed ID word and comparison measurements; where these cannot decide, '
              'the result is shown as a range.']:
        put(r, 1, '- ' + t, 'i_none'); r += 1
    widths = [16, 7, 62, 21, 21, 9, 7, 7, 8, 8, 8, 6, 6, 6, 6, 6, 6, 6, 9, 26, 24, 34, 60]
    add_sheet(traveller_path, out_path, 'STDF Fill-in', C, widths, freeze=(hdr_row + 1, 4), heights=heights)
    return rows
