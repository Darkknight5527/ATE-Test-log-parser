"""Full STDF V4 reader used for the per-file datalog sheets (every test of every run, with the run's X/Y, test time,
failing tests in order, bins, MIR header details). The lot counting itself uses stdf_lot_yield_summary.py."""
import datetime, gzip, hashlib, re, struct
from collections import OrderedDict


def g7(v):
    """STDF stores R4 (float32): keep 7 significant digits so -600.000024 doesn't show as -600.0000238..."""
    return None if v is None else float('%.7g' % v)


PREFIX = {15: 'f', 12: 'p', 9: 'n', 6: 'u', 3: 'm', 2: '%', 0: '', -3: 'k', -6: 'M', -9: 'G', -12: 'T'}


class Rd:
    """Little helper that reads STDF fields from one record body; returns None past the end."""
    def __init__(s, b): s.b, s.o = b, 0
    def _u(s, fmt, n):
        if s.o + n > len(s.b): s.o = len(s.b); return None
        v = struct.unpack_from('<' + fmt, s.b, s.o)[0]; s.o += n; return v
    def u1(s): return s._u('B', 1)
    def u2(s): return s._u('H', 2)
    def u4(s): return s._u('I', 4)
    def i1(s): return s._u('b', 1)
    def i2(s): return s._u('h', 2)
    def i4(s): return s._u('i', 4)
    def r4(s): return s._u('f', 4)
    def cn(s):
        n = s.u1()
        if n is None: return ''
        v = s.b[s.o:s.o + n].decode('latin1'); s.o += n; return v
    def skip(s, n): s.o = min(len(s.b), s.o + n)


def read_stdf(path):
    raw = open(path, 'rb').read()
    if raw[:2] == b'\x1f\x8b':
        raw = gzip.decompress(raw)
    if len(raw) < 6 or raw[2:4] != b'\x00\x0a':
        raise ValueError('not an STDF file (no FAR record at start)')
    if raw[4] != 2:
        raise ValueError('only little-endian STDF (CPU_TYPE 2, V93000) is supported')
    f = dict(path=path, md5=hashlib.md5(raw).hexdigest(), mir={}, runs=[], tests=OrderedDict(),
             hbr={}, sbr={}, pcr=None, finish=None)
    tests = f['tests']
    first_lim = {}                     # test number -> (lo, hi, unit, scale) from first PTR
    open_runs = {}                     # (head, site) -> run dict
    i, n = 0, len(raw)
    while i + 4 <= n:
        ln, typ, sub = struct.unpack_from('<HBB', raw, i)
        b = raw[i + 4:i + 4 + ln]; i += 4 + ln
        r = Rd(b)
        if (typ, sub) == (1, 10):                                     # MIR
            setup, start = r.u4(), r.u4(); r.u1(); r.skip(1 + 1 + 1); r.u2(); r.skip(1)
            names = ['lot', 'part', 'node', 'tester_type', 'job', 'job_rev', 'sblot', 'oper',
                     'exec_type', 'exec_ver', 'test_cod', 'tst_temp']
            f['mir'] = {k: r.cn() for k in names}
            f['mir']['start'] = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=start or 0)
        elif (typ, sub) == (5, 10):                                   # PIR
            h, s = r.u1(), r.u1()
            open_runs[(h, s)] = dict(head=h, site=s, vals={}, fails=[])
        elif (typ, sub) == (15, 10):                                  # PTR
            tn, h, s, tf, pf, res = r.u4(), r.u1(), r.u1(), r.u1(), r.u1(), r.r4()
            txt = r.cn(); r.cn()
            opt = r.u1()
            run = open_runs.get((h, s))
            if opt is not None:
                rs, ls, hs = r.i1(), r.i1(), r.i1()
                lo, hi = r.r4(), r.r4(); unit = r.cn()
                if tn not in first_lim:
                    lo = None if (opt & 0x40 or opt & 0x10) else lo
                    hi = None if (opt & 0x80 or opt & 0x20) else hi
                    first_lim[tn] = (lo, hi, unit, rs if not (opt & 0x01) else 0, ls or 0, hs or 0)
            key = (tn, re.sub(r'\[\d+\]$', '', txt))
            if key not in tests:
                lo, hi, unit, rs, ls, hs = first_lim.get(tn, (None, None, '', 0, 0, 0))
                tests[key] = dict(num=tn, name=txt, kind='P', scale=rs,
                                  unit=PREFIX.get(rs, 'e%d ' % -rs) + unit,
                                  lo=None if lo is None else g7(lo * 10 ** ls),
                                  hi=None if hi is None else g7(hi * 10 ** hs), pos=len(tests) + 1)
            if run is not None:
                t = tests[key]
                if key not in run['vals'] and res is not None and not (tf & 0x02):
                    run['vals'][key] = g7(res * 10 ** t['scale'])
                if tf is not None and tf & 0x80:
                    run['fails'].append(key)
        elif (typ, sub) == (15, 20):                                  # FTR (functional)
            tn, h, s, tf = r.u4(), r.u1(), r.u1(), r.u1()
            r.skip(1 + 4 * 6 + 2)
            rtn, pgm = r.u2() or 0, None
            r.skip(2 * rtn + (rtn + 1) // 2)
            pgm = r.u2() or 0
            r.skip(2 * pgm + (pgm + 1) // 2)
            bits = r.u2() or 0; r.skip((bits + 7) // 8)
            r.cn(); r.cn(); r.cn(); txt = r.cn()
            key = (tn, re.sub(r'\[\d+\]$', '', txt))
            if key not in tests:
                tests[key] = dict(num=tn, name=txt, kind='F', scale=0, unit='P/F', lo=None,
                                  hi=None, pos=len(tests) + 1)
            run = open_runs.get((h, s))
            if run is not None:
                failed = bool(tf is not None and tf & 0x80)
                run['vals'].setdefault(key, 'F' if failed else 'P')
                if failed: run['fails'].append(key)
        elif (typ, sub) == (15, 15):                                  # MPR (pass/fail only)
            tn, h, s, tf = r.u4(), r.u1(), r.u1(), r.u1()
            r.u1(); rtn = r.u2() or 0; rsl = r.u2() or 0
            r.skip((rtn + 1) // 2 + 4 * rsl); txt = r.cn()
            key = (tn, re.sub(r'\[\d+\]$', '', txt))
            if key not in tests:
                tests[key] = dict(num=tn, name=txt, kind='M', scale=0, unit='P/F', lo=None,
                                  hi=None, pos=len(tests) + 1)
            run = open_runs.get((h, s))
            if run is not None:
                failed = bool(tf is not None and tf & 0x80)
                run['vals'].setdefault(key, 'F' if failed else 'P')
                if failed: run['fails'].append(key)
        elif (typ, sub) == (5, 20):                                   # PRR
            h, s, pf, nt, hb, sb, x, y, tt = (r.u1(), r.u1(), r.u1(), r.u2(), r.u2(), r.u2(),
                                              r.i2(), r.i2(), r.u4())
            pid = r.cn()
            run = open_runs.pop((h, s), dict(head=h, site=s, vals={}, fails=[]))
            run.update(sn=pid.strip() or '?', flag=pf or 0, hb=hb, sb=sb, x=x, y=y, tt=tt or 0,
                       ntest=nt, failed=bool((pf or 0) & 0x08), tester_retest=bool((pf or 0) & 0x01))
            run['run'] = len(f['runs']) + 1
            f['runs'].append(run)
        elif (typ, sub) in ((1, 40), (1, 50)):                        # HBR / SBR
            h, s, num, cnt = r.u1(), r.u1(), r.u2(), r.u4()
            pf = r.b[8:9].decode('latin1') if len(r.b) > 8 else ''; r.skip(1); nm = r.cn()
            if h == 255:
                (f['hbr'] if sub == 40 else f['sbr'])[num] = (cnt, pf, nm)
        elif (typ, sub) == (1, 20):                                   # MRR
            fin = r.u4()
            f['finish'] = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=fin) if fin else None
        elif (typ, sub) == (1, 30):                                   # PCR
            h, s, pc, rc, ab, gd = r.u1(), r.u1(), r.u4(), r.u4(), r.u4(), r.u4()
            if h == 255 or f['pcr'] is None:
                f['pcr'] = dict(parts=pc, retests=rc, good=gd)
    return f
