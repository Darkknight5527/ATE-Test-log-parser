"""Per-file sheet in the classic datalog-viewer layout (header, yield-fail summary, reject pareto, Cpk,
outlier summary, statistics block, FAIL UNITS / PASS UNITS list with every test value)."""
from collections import defaultdict
import os
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter as L

# --------------------------------------------------------------------------- styles
AR = Font(name='Arial', size=10)
ARB = Font(name='Arial', size=10, bold=True)
WH = Font(name='Arial', size=10, bold=True, color='FFFFFF')
SEC = Font(name='Arial', size=11, bold=True, color='104861')
NOTE = Font(name='Arial', size=9, italic=True, color='595959')
LINK = Font(name='Arial', size=10, color='0563C1', underline='single')
HDR = PatternFill('solid', fgColor='104861')
GREY = PatternFill('solid', fgColor='D9D9D9')
STAT = PatternFill('solid', fgColor='DDEBF7')
GRN = PatternFill('solid', fgColor='C6EFCE')
AMB = PatternFill('solid', fgColor='FFE699')
RED = PatternFill('solid', fgColor='F8CBAD')
CENTER = Alignment(horizontal='center', vertical='center', wrap_text=True)


def put(ws, r, c, v, font=AR, fill=None, fmt=None, align=None):
    x = ws.cell(r, c, v); x.font = font
    if fill: x.fill = fill
    if fmt: x.number_format = fmt
    if align: x.alignment = align
    return x


def header_row(ws, r, labels, c0=1):
    for k, h in enumerate(labels):
        put(ws, r, c0 + k, h, WH, HDR, align=CENTER)


def code(run, tests):
    if not run['failed']: return 'P'
    if run['fails']:
        t = tests[run['fails'][0]]
        return 'F%04d_T%d' % (t['pos'], t['num'])
    return 'F_HB%d' % run['hb']


# --------------------------------------------------------------------------- per-file sheet
UNIT_COLS = ['Unit SN', 'Pass/Fail', 'SBin', 'HBin', 'Site', 'X coord', 'Y coord', 'Time',
             'Test Time [s]', 'Retest ID', 'Retest Cnt', 'First Fail', 'Retest Result',
             'Retest Analysis']
BASE_STATS = ['Test Number', 'Test Name', 'Test Unit', 'Low Limit', 'High Limit', '<LoLim Count',
              '>HiLim Count', 'Avg', 'StDev', 'Min', 'Max', 'Range', 'Cp', 'Cpk', 'Fail.First Count',
              'Fail.All Count', 'Pass Count', 'Multiple Dist. Flag', 'Outlier Flag', '<Avg-6sigma',
              '>Avg+6sigma', 'Retest Recovered Count', 'p05%', 'p25%', 'Median', 'p75%', 'p95%',
              'Avg-3sigma', 'Avg+3sigma', 'Process tol(%)', 'Control tol(%)', 'Original TF']
VALUE_ROWS = {'Test Number', 'Test Name', 'Test Unit', 'Low Limit', 'High Limit', 'Fail.First Count',
              'Fail.All Count', 'Pass Count', 'Retest Recovered Count', 'Original TF'}
FT = len(UNIT_COLS) + 1            # first test column (O)


CONTI = ['CONTINUITY']


def is_conti(t):
    return bool(CONTI[0]) and t['name'].split(':')[0].upper().startswith(CONTI[0].upper())


def file_sheet(wb, f, sheet, info, stats_on):
    ws = wb.create_sheet(sheet)
    tests, runs, m = f['tests'], f['runs'], f['mir']
    keys = list(tests)
    sites = sorted({x['site'] for x in runs}) or [1]
    STATS = BASE_STATS + [('S%d_%s' % (st, n)) for st in sites for n in ('Avg', 'StDev', 'Cp', 'Cpk', 'Unit Cnt')]
    by_sn = info['by_sn']

    # ---------- Python-side pre-calculation (only to decide what is listed; cells stay formulas)
    pop = [x for x in runs if (stats_on == 'all' or not x['failed'])]
    fail_first = defaultdict(int)
    for run in runs:
        if run['failed'] and run['fails']: fail_first[run['fails'][0]] += 1
    pareto = sorted(fail_first.items(), key=lambda kv: -kv[1])
    low_cpk, mdist_rows = [], []
    for k in keys:
        t = tests[k]
        if t['kind'] != 'P': continue
        v = [x['vals'][k] for x in pop if isinstance(x['vals'].get(k), float)]
        if len(v) < 2: continue
        mu = sum(v) / len(v); sd = (sum((a - mu) ** 2 for a in v) / (len(v) - 1)) ** .5
        if sd > 0 and t['lo'] is not None and t['hi'] is not None:
            if min(t['hi'] - mu, mu - t['lo']) / (3 * sd) < 2.0: low_cpk.append(k)
        outl = sum(1 for a in v if a < mu - 6 * sd or a > mu + 6 * sd) if sd > 0 else 0
        md = False
        if len(sites) > 1 and sd > 0:
            sv = []
            for st in sites:
                w = [x['vals'][k] for x in pop if x['site'] == st and isinstance(x['vals'].get(k), float)]
                if len(w) > 1:
                    mw = sum(w) / len(w); sv.append(sum((a - mw) ** 2 for a in w) / (len(w) - 1))
            md = bool(sv) and sd > 1.5 * (sum(sv) / len(sv)) ** .5
        if outl or md: mdist_rows.append(k)
    low_cpk, mdist_rows = low_cpk[:500], mdist_rows[:500]
    recovered = defaultdict(int)          # test -> units that failed it earlier and passed on last run
    for sn, hist in by_sn.items():
        if len(hist) > 1 and not hist[-1]['failed']:
            for k in {kk for x in hist[:-1] for kk in x['fails']}: recovered[k] += 1

    # ---------- row plan
    ysum = 16                              # Summary sheet relies on this row
    ny = 10
    par0 = ysum + ny + 3
    cpk0 = par0 + max(len(pareto), 1) + 3
    md0 = cpk0 + max(len(low_cpk), 1) + 3
    path_r = md0 + max(len(mdist_rows), 1) + 2
    strip = path_r + 3                     # Stat / Lot / EBR# strip
    st0 = strip + 1
    srow = {name: st0 + k for k, name in enumerate(STATS)}
    hdr = st0 + len(STATS)
    fails = [x for x in runs if x['failed']]
    passes = [x for x in runs if not x['failed']]
    frow0 = hdr + 2
    frows = list(range(frow0, frow0 + len(fails)))
    prow_label = frow0 + len(fails) + 1
    prows = list(range(prow_label + 1, prow_label + 1 + len(passes)))
    allrows = frows + prows
    lo, hi = (min(allrows), max(allrows)) if allrows else (hdr + 1, hdr + 1)
    plo, phi = (prows[0], prows[-1]) if prows else (lo, hi)
    s_lo, s_hi = (lo, hi) if stats_on == 'all' else (plo, phi)
    no_pop = (stats_on == 'pass' and not prows) or not allrows
    J = lambda c: '$%s$%d:$%s$%d' % (c, lo, c, hi)

    # ---------- header
    put(ws, 1, 1, '### Raw DataLog Header Information ###', SEC)
    r = 2
    for k, v in [('File', os.path.basename(f['path'])), ('Lot', m.get('lot')), ('Sublot', m.get('sblot')),
                 ('Device / Part type', m.get('part')), ('Program', m.get('job')),
                 ('Temperature (from program/lot name)', info['temp']),
                 ('Start time (tester clock)', m['start'].strftime('%Y-%m-%d %H:%M:%S') if m.get('start') else ''),
                 ('Tester', m.get('node')), ('Tester type', m.get('tester_type')), ('Operator', m.get('oper')),
                 ('Shell rev.', ('%s %s' % (m.get('exec_type', ''), m.get('exec_ver', ''))).strip()),
                 ('Test code', m.get('test_cod'))]:
        put(ws, r, 2, k, ARB); put(ws, r, 3, v); r += 1
    for rr_, txt_, tgt in ((1, 'Jump to unit list / test values', hdr), (2, 'Jump to stat block', st0)):
        x_ = put(ws, rr_, 5, txt_, LINK); x_.hyperlink = "#'%s'!A%d" % (sheet, tgt)

    # ---------- YIELD-FAIL SUMMARY  (C = all sites, D.. = per site)
    put(ws, ysum - 1, 1, '### YIELD-FAIL SUMMARY ###', SEC)
    put(ws, ysum - 1, 2, 'Site:', ARB, align=Alignment(horizontal='right'))
    put(ws, ysum - 1, 3, 'Sall', WH, HDR, align=CENTER)
    conti_codes = ['F%04d_T%d' % (tests[k]['pos'], tests[k]['num']) for k, _ in pareto if is_conti(tests[k])]
    for ci, st in enumerate([None] + sites):
        col = 3 + ci; C = L(col)
        if st is not None: put(ws, ysum - 1, col, 'S%d' % st, WH, HDR, align=CENTER)
        cs = '' if st is None else ',%s,%d' % (J('E'), st)
        ce = '' if st is None else '*(%s=%d)' % (J('E'), st)
        conti = '+'.join('COUNTIFS(%s,"%s"%s)' % (J('B'), cd, cs) for cd in conti_codes) or '0'
        y = lambda o: '%s%d' % (C, ysum + o)
        fm = ['=COUNTIFS(%s,">0"%s)' % (J('J'), cs),
              '=COUNTIFS(%s,"P",%s,">0"%s)' % (J('B'), J('J'), cs),
              '=%s-%s' % (y(0), y(1)),
              '=%s' % conti,
              '=IF(%s=0,0,%s/%s)' % (y(0), y(1), y(0)),
              '=IF(%s-%s=0,0,%s/(%s-%s))' % (y(0), y(3), y(1), y(0), y(3)),
              '=COUNTIFS(%s,1%s)' % (J('J'), cs),
              '=COUNTIFS(%s,1,%s,">1"%s)' % (J('J'), J('K'), cs),
              '=SUMPRODUCT(ISNUMBER(%s)*(%s=%s)*(%s="P")%s)' % (J('K'), J('J'), J('K'), J('B'), ce),
              '=IF(%s=0,0,%s/%s)' % (y(6), y(8), y(6))]
        for o, fx in enumerate(fm):
            put(ws, ysum + o, col, fx, fmt='0.00%' if o in (4, 5, 9) else None)
    labels = ['In (runs, incl. retests)', 'Pass', 'Fail', 'FailConti (first fail = CONTINUITY)', 'Yield [%] (per run)',
              'Yield w/o Conti [%]', 'Unique units (Unit SN)', 'Units retested in this file',
              'Units passing on their last run', 'Final yield per unit [%]']
    for o, lab in enumerate(labels): put(ws, ysum + o, 2, lab, ARB)
    nc = 3 + len(sites) + 2
    good = f['hbr'] and sum(c for c, p, _ in f['hbr'].values() if p == 'P')
    put(ws, ysum, nc, 'Tester summary records:', NOTE)
    put(ws, ysum + 1, nc, 'HBR pass-bin count = %s' % (good if f['hbr'] else 'n/a'), NOTE)
    if f['pcr']:
        put(ws, ysum + 2, nc, 'PCR parts = %d, retests = %d, good = %d'
            % (f['pcr']['parts'], f['pcr']['retests'], f['pcr']['good']), NOTE)
    put(ws, ysum + 3, nc, 'Bins: ' + (', '.join('HB%d %s x%d' % (n, nm or p, c)
                                              for n, (c, p, nm) in sorted(f['hbr'].items())) or 'n/a'), NOTE)

    # ---------- REJECT PARETO
    put(ws, par0 - 2, 1, '### REJECT PARETO (first failing test per failed run) ###', SEC)
    header_row(ws, par0 - 1, ['Test #', 'Test Description', 'Unit', 'Rejects', 'Rejects[%]'] +
               ['S%d' % st for st in sites], 1)
    for k, (key, _) in enumerate(pareto):
        t = tests[key]; rr = par0 + k; cd = 'F%04d_T%d' % (t['pos'], t['num'])
        put(ws, rr, 1, t['num']); put(ws, rr, 2, t['name']); put(ws, rr, 3, t['unit'])
        put(ws, rr, 4, '=COUNTIF(%s,"%s")' % (J('B'), cd))
        put(ws, rr, 5, '=IF($C$%d=0,0,D%d/$C$%d)' % (ysum, rr, ysum), fmt='0.0%')
        for si, st in enumerate(sites):
            put(ws, rr, 6 + si, '=COUNTIFS(%s,"%s",%s,%d)' % (J('B'), cd, J('E'), st))
    if not pareto: put(ws, par0, 1, 'No failed runs', NOTE)

    col_of = {k: FT + j for j, k in enumerate(keys)}
    # ---------- CPK SUMMARY
    put(ws, cpk0 - 2, 1, '### CPK Summary (tests with Cp / Cpk < 2.0, first 500; on %s units) ###'
        % ('PASS' if stats_on == 'pass' else 'ALL'), SEC)
    header_row(ws, cpk0 - 1, ['Test #', 'Test Description', 'Unit', 'Cpk_All', 'Cp'] +
               ['S%d Cpk' % st for st in sites], 1)
    for k, key in enumerate(low_cpk):
        t = tests[key]; rr = cpk0 + k; c = L(col_of[key])
        put(ws, rr, 1, t['num']); put(ws, rr, 2, t['name']); put(ws, rr, 3, t['unit'])
        put(ws, rr, 4, '=%s%d' % (c, srow['Cpk']), fmt='0.000')
        put(ws, rr, 5, '=%s%d' % (c, srow['Cp']), fmt='0.000')
        for si, st in enumerate(sites):
            put(ws, rr, 6 + si, '=%s%d' % (c, srow['S%d_Cpk' % st]), fmt='0.000')
    if not low_cpk: put(ws, cpk0, 1, 'No test below Cpk 2.0 (or not enough units)', NOTE)

    # ---------- MULTIPLE DISTRIBUTION / OUTLIER SUMMARY
    put(ws, md0 - 2, 1, '### Multiple Distribution Over Sites and Outlier Summary (first 500 affected tests) ###', SEC)
    header_row(ws, md0 - 1, ['Test #', 'Test Description', 'Unit', 'Cpk', 'M-Dist', 'Outliers'], 1)
    for k, key in enumerate(mdist_rows):
        t = tests[key]; rr = md0 + k; c = L(col_of[key])
        put(ws, rr, 1, t['num']); put(ws, rr, 2, t['name']); put(ws, rr, 3, t['unit'])
        put(ws, rr, 4, '=%s%d' % (c, srow['Cpk']), fmt='0.000')
        put(ws, rr, 5, '=%s%d' % (c, srow['Multiple Dist. Flag']))
        put(ws, rr, 6, '=IFERROR(%s%d+%s%d,"x")' % (c, srow['<Avg-6sigma'], c, srow['>Avg+6sigma']))
    if not mdist_rows:
        put(ws, md0, 1, 'No test with values beyond Avg +/- 6 sigma%s' %
            (' or site-to-site spread' if len(sites) > 1 else ' (single site: M-Dist needs 2+ sites)'), NOTE)

    # ---------- DATALOG FILE PATH
    put(ws, path_r - 1, 1, '### Datalog File Path ###', SEC)
    put(ws, path_r, 2, f['path'])
    x_ = put(ws, path_r + 1, 1, 'Back to top', LINK); x_.hyperlink = "#'%s'!A1" % sheet

    # ---------- Stat / Lot strip + DATALOG INFO (like the viewer)
    strip_lab = ['Stat', 'Lot/SubLot', 'EBR#', 'Qual Lot', 'Yield', 'Dev OPN', 'Prog rev.', 'TestMode',
                 'Temperature', 'HW#', 'Date', 'CAB flag', 'Note', 'Note2']
    for c, h in enumerate(strip_lab, 1):
        put(ws, strip, c, h, WH, HDR, align=CENTER)
    sv = ['%s/%s' % (m.get('lot', ''), m.get('sblot', '')), '.', '.', '=C%d' % (ysum + 4), m.get('part'),
          ('%s %s' % (m.get('job', ''), m.get('job_rev', ''))).strip(), m.get('test_cod'), info['temp'], '.',
          m['start'].strftime('%d-%b-%Y %H:%M') if m.get('start') else '', '.', '.', '.']
    for c, v in enumerate(sv, 2):
        put(ws, st0, c, v, fmt='0.00%' if c == 5 else None)
    di = [('DATALOG INFO:', None), ('Device:', m.get('part')), ('Test Mode:', m.get('test_cod')),
          ('Temperature:', info['temp']), ('Lot:', m.get('lot')), ('Sublot:', m.get('sblot')),
          ('Date, Time:', m['start'].strftime('%H:%M:%S %d-%b-%Y') if m.get('start') else ''),
          ('Platform:', m.get('tester_type')), ('Tester id:', m.get('node')),
          ('Shell rev.:', ('%s %s' % (m.get('exec_type', ''), m.get('exec_ver', ''))).strip()),
          ('Prog. name:', m.get('job')), ('Prog. rev.:', m.get('job_rev')), ('Operator:', m.get('oper'))]
    for k, (lab, v) in enumerate(di):
        put(ws, srow['Fail.First Count'] + k, 7, lab, ARB); put(ws, srow['Fail.First Count'] + k, 8, v)

    # ---------- stat block labels
    for name, rr in srow.items():
        put(ws, rr, 1, name, ARB, STAT)
    put(ws, srow['Avg'], 3, 'Stats on: %s units' % ('PASS' if stats_on == 'pass' else 'ALL'), NOTE)
    put(ws, srow['Avg'] + 1, 3, 'rows %d-%d' % (s_lo, s_hi), NOTE)

    # ---------- unit list
    header_row(ws, hdr, UNIT_COLS)
    for j, k in enumerate(keys):
        put(ws, hdr, FT + j, 'T%d' % tests[k]['num'], WH, HDR, align=CENTER)
    last_col = FT + len(keys) - 1 if keys else len(UNIT_COLS)
    for c in range(1, last_col + 1):
        put(ws, frow0 - 1, c, 'FAIL UNITS' if c == 1 else 'F', ARB, GREY)
        put(ws, prow_label, c, 'PASS UNITS' if c == 1 else 'P', ARB, GREY)
    for run, rr in zip(fails + passes, frows + prows):
        hist = by_sn[run['sn']]
        firstf = next((code(x, tests) for x in hist if x['failed']), '.')
        sn = int(run['sn']) if run['sn'].isdigit() else run['sn']
        vals = [sn, code(run, tests), run['sb'], run['hb'], run['site'], run['x'], run['y'],
                '%dms' % run['tt'], round(run['tt'] / 1000.0, 3), hist.index(run) + 1,
                '=COUNTIF($A$%d:$A$%d,A%d)' % (lo, hi, rr), firstf,
                'F' if hist[-1]['failed'] else 'P', info['analysis'][id(run)]]
        for c, v in enumerate(vals, 1):
            put(ws, rr, c, v)
        for j, k in enumerate(keys):
            v = run['vals'].get(k)
            if v is not None:
                ws.cell(rr, FT + j, v).font = AR
    for j, k in enumerate(keys):
        t = tests[k]; c = L(FT + j)
        if t['kind'] != 'P': continue
        conds = []
        if t['lo'] is not None: conds.append('%s%d<%s$%d' % (c, lo, c, srow['Low Limit']))
        if t['hi'] is not None: conds.append('%s%d>%s$%d' % (c, lo, c, srow['High Limit']))
        if conds:
            ws.conditional_formatting.add('%s%d:%s%d' % (c, lo, c, hi), FormulaRule(
                formula=['AND(ISNUMBER(%s%d),OR(%s))' % (c, lo, ','.join(conds))], fill=RED))
    if keys:
        ws.conditional_formatting.add('%s%d:%s%d' % (L(FT), lo, L(last_col), hi),
                                      CellIsRule(operator='equal', formula=['"F"'], fill=RED))
    rng = 'A%d:N%d' % (lo, hi)
    ws.conditional_formatting.add(rng, FormulaRule(
        formula=['AND(ISNUMBER($K%d),$K%d>1,$M%d="P")' % (lo, lo, lo)], fill=GRN))
    ws.conditional_formatting.add(rng, FormulaRule(
        formula=['AND(ISNUMBER($K%d),$K%d>1)' % (lo, lo)], fill=AMB))
    ws.conditional_formatting.add(rng, FormulaRule(
        formula=['ISNUMBER(SEARCH("also tested in",$N%d))' % lo], fill=AMB))

    # ---------- stat block per test
    E_s = '$E$%d:$E$%d' % (s_lo, s_hi)
    for j, k in enumerate(keys):
        t = tests[k]; cc = FT + j; c = L(cc)
        R = '%s%d:%s%d' % (c, s_lo, c, s_hi); A_ = '%s%d:%s%d' % (c, lo, c, hi)
        S = lambda name: '%s%d' % (c, srow[name])
        put(ws, srow['Test Number'], cc, 'T%d' % t['num'], ARB)
        put(ws, srow['Test Name'], cc, t['name'], align=Alignment(wrap_text=True, vertical='top'))
        put(ws, srow['Test Unit'], cc, t['unit'])
        put(ws, srow['Low Limit'], cc, t['lo'] if t['lo'] is not None else '.')
        put(ws, srow['High Limit'], cc, t['hi'] if t['hi'] is not None else '.')
        put(ws, srow['Original TF'], cc, t['pos'])
        fa = sum(1 for x in runs if k in x['fails'])
        put(ws, srow['Fail.First Count'], cc, fail_first.get(k, 0)); put(ws, srow['Fail.All Count'], cc, fa)
        put(ws, srow['Pass Count'], cc, sum(1 for x in runs if k in x['vals']) - fa)
        put(ws, srow['Retest Recovered Count'], cc, recovered.get(k, 0))
        if t['kind'] != 'P' or no_pop:
            for name in STATS:
                if name not in VALUE_ROWS: put(ws, srow[name], cc, 'x' if 'Flag' not in name else '.')
            continue
        put(ws, srow['<LoLim Count'], cc, '=IF(ISNUMBER(%s),COUNTIF(%s,"<"&%s),"x")' % (S('Low Limit'), A_, S('Low Limit')))
        put(ws, srow['>HiLim Count'], cc, '=IF(ISNUMBER(%s),COUNTIF(%s,">"&%s),"x")' % (S('High Limit'), A_, S('High Limit')))
        put(ws, srow['Avg'], cc, '=IF(COUNT(%s)>0,AVERAGE(%s),"x")' % (R, R))
        put(ws, srow['StDev'], cc, '=IF(OR(%s="x",COUNT(%s)<2),"x",STDEV(%s))' % (S('Avg'), R, R))
        put(ws, srow['Min'], cc, '=IF(%s="x","x",MIN(%s))' % (S('Avg'), R))
        put(ws, srow['Max'], cc, '=IF(%s="x","x",MAX(%s))' % (S('Avg'), R))
        put(ws, srow['Range'], cc, '=IF(%s="x","x",%s-%s)' % (S('Avg'), S('Max'), S('Min')))
        def cp_cpk(avg, sd):
            lim = 'AND(ISNUMBER(%s),ISNUMBER(%s),ISNUMBER(%s))' % (S('Low Limit'), S('High Limit'), sd)
            return ('=IF(NOT(%s),"x",IF(%s=0,999.9,(%s-%s)/(6*%s)))' % (lim, sd, S('High Limit'), S('Low Limit'), sd),
                    '=IF(NOT(%s),"x",IF(%s=0,999.9,MIN(%s-%s,%s-%s)/(3*%s)))'
                    % (lim, sd, S('High Limit'), avg, avg, S('Low Limit'), sd))
        cpf, cpkf = cp_cpk(S('Avg'), S('StDev'))
        put(ws, srow['Cp'], cc, cpf); put(ws, srow['Cpk'], cc, cpkf)
        site_sd = ','.join(S('S%d_StDev' % st) for st in sites)
        put(ws, srow['Multiple Dist. Flag'], cc, '=IFERROR(IF(%s>1.5*SQRT(SUMSQ(%s)/%d),"F","."),".")'
            % (S('StDev'), site_sd, len(sites)))
        put(ws, srow['Outlier Flag'], cc, '=IF(%s="x",".",IF(%s+%s>0,"F","."))'
            % (S('StDev'), S('<Avg-6sigma'), S('>Avg+6sigma')))
        put(ws, srow['<Avg-6sigma'], cc, '=IF(ISNUMBER(%s),COUNTIF(%s,"<"&(%s-6*%s)),"x")' % (S('StDev'), R, S('Avg'), S('StDev')))
        put(ws, srow['>Avg+6sigma'], cc, '=IF(ISNUMBER(%s),COUNTIF(%s,">"&(%s+6*%s)),"x")' % (S('StDev'), R, S('Avg'), S('StDev')))
        for name, p in (('p05%', .05), ('p25%', .25), ('Median', .5), ('p75%', .75), ('p95%', .95)):
            put(ws, srow[name], cc, '=IF(%s="x","x",PERCENTILE(%s,%s))' % (S('Avg'), R, p))
        put(ws, srow['Avg-3sigma'], cc, '=IF(ISNUMBER(%s),%s-3*%s,"x")' % (S('StDev'), S('Avg'), S('StDev')))
        put(ws, srow['Avg+3sigma'], cc, '=IF(ISNUMBER(%s),%s+3*%s,"x")' % (S('StDev'), S('Avg'), S('StDev')))
        # same formulas as the viewer export (Process tol uses Min, as there)
        put(ws, srow['Process tol(%)'], cc, '=IFERROR(IF(%s="x","x",ABS(%s*0.5/%s)),"x")' % (S('Avg'), S('Min'), S('Avg')), fmt='0.00%')
        put(ws, srow['Control tol(%)'], cc, '=IFERROR(IF(ISNUMBER(%s),ABS(3*%s/%s),"x"),"x")' % (S('StDev'), S('StDev'), S('Avg')), fmt='0.00%')
        for st in sites:
            n = lambda nm: 'S%d_%s' % (st, nm)
            put(ws, srow[n('Avg')], cc, '=IFERROR(AVERAGEIF(%s,%d,%s),"x")' % (E_s, st, R))
            put(ws, srow[n('StDev')], cc, '=IFERROR(SQRT((SUMPRODUCT((%s=%d)*ISNUMBER(%s),%s,%s)-COUNTIFS(%s,%d,%s,"<>")*%s^2)'
                '/(COUNTIFS(%s,%d,%s,"<>")-1)),"x")' % (E_s, st, R, R, R, E_s, st, R, S(n('Avg')), E_s, st, R))
            cpf, cpkf = cp_cpk(S(n('Avg')), S(n('StDev')))
            put(ws, srow[n('Cp')], cc, cpf); put(ws, srow[n('Cpk')], cc, cpkf)
            put(ws, srow[n('Unit Cnt')], cc, '=COUNTIFS(%s,%d,%s,"<>")' % (E_s, st, R))

    # ---------- layout
    for c, w in enumerate([22, 60, 9, 10, 10, 9, 14, 30, 10, 8, 8, 16, 9, 34], 1):
        ws.column_dimensions[L(c)].width = w
    for j in range(len(keys)):
        ws.column_dimensions[L(FT + j)].width = 13
    ws.row_dimensions[hdr].height = 30
    ws.row_dimensions[srow['Test Name']].height = 60
    ws.freeze_panes = 'C1'      # columns A:B stay visible; rows scroll freely
    note = (hi if allrows else hdr) + 2
    for k, t in enumerate([
            'Legend: Amber = Unit SN tested more than once (in this file or another file) and still failing on its last run;'
            ' green = retested and passed on its last run. Red value = outside limits / functional fail.',
            'Retest ID = 1st/2nd/3rd time this Part ID appears in this file. Retest Cnt = times in this file (formula).'
            ' Retest Analysis = what the lot tracing found for this part (see Lot Summary / Exceptions).',
            'Pass/Fail code F<flow position>_T<test number> = first failing test of the run. Values are in display units'
            ' (value x 10^RES_SCAL), e.g. AIN with scale -9 shows as GAIN.']):
        put(ws, note + k, 1, t, NOTE)
    return ws


