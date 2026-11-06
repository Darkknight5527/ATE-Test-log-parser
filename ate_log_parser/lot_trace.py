"""Lot tracing: follow every physical part through all files of one lot and count it once.

Rules
  * a unit = rows of the same Part ID next to each other in one file (tester retest); its last result counts
  * Part ID restarts in every file, so it never links files
  * a unit 'arrives programmed' if its ID read-back is non-zero -> it carries an ID word written earlier and is linked to
    the earlier unit(s) with that word; several parts can share a word, then the comparison measurements decide
    (same stage only)
  * units arriving unprogrammed (read-back 0) or without a read (failed before the read) are anonymous: in a retest file
    they are matched by COUNT to earlier fails at that stage (each earlier fail at most once per file); more than that = EXTRA
  * every good part is programmed, so passes are always identifiable; anonymous parts never change the good count
"""
import collections, os, re, statistics as st, struct

f32 = lambda w: struct.unpack('<f', struct.pack('<f', float(w)))[0]
key7 = lambda x: '%.7g' % x


def device_of(f, profile):
    rx = profile.get('device_regex')
    job = f['mir'].get('job', '') or ''
    if rx:
        m = re.search(rx, job)
        if m: return m.group(1)
    return re.sub(r'\.\w+$', '', job) or '?'


def tags_of(f, profile):
    s = (os.path.basename(f['path']) + ' ' + (f['mir'].get('lot') or '')).upper()
    tags = [t for t in profile.get('excluded_tags', []) if re.search(r'(^|[_\W])' + re.escape(t.upper()) + r'($|[_\W\d])', s)]
    if profile.get('retest_regex') and re.search(profile['retest_regex'], s, re.I): tags.append('RETEST-name')
    allp = ' '.join([f['path']] + f.get('other_paths', [])).upper()
    if profile.get('bake_regex') and re.search(profile['bake_regex'], s + ' ' + allp, re.I): tags.append('POST-BAKE')
    return tags


def temperature_of(f, profile):
    s = (f['mir'].get('job', '') or '') + ' ' + os.path.basename(f['path'])
    for stage, rx in profile.get('temperature_rules', []):
        if re.search(rx, s, re.I): return stage
    return 'Room'


class Unit:
    __slots__ = ('dev', 'ins', 'stage', 'file', 'fi', 'idx', 'pid', 'runs', 'res', 'hb', 'sb', 'arr', 'arr_key', 'word_key',
                 'post', 'job', 'pp', 'cls', 'link', 'link_unit', 'amb', 'dist', 'tags', 'first_fail', 'run_list', '_d')

    def when(self):
        return self.file['mir']['start'].strftime('%m-%d %H:%M') if self.file['mir'].get('start') else '?'

    def ref(self):
        return 'file %s Part ID %s (%s %s)' % (self.when(), self.pid, self.stage, self.res)


def _spread(files):
    cols = collections.defaultdict(lambda: collections.defaultdict(list))
    for f in files:
        for r in f['runs']:
            for k, v in r.get('post', {}).items(): cols[f['mir'].get('job')][k].append(v)
    out = {}
    for job, T in cols.items():
        out[job] = {}
        for k, v in T.items():
            if len(v) >= 10:
                v = sorted(v); iqr = (v[3 * len(v) // 4] - v[len(v) // 4]) / 1.349
                if iqr > 0: out[job][k] = iqr
    return out


def trace(files, profile):
    """files: fingerprinted file dicts of ONE device. Returns (files sorted, units, role per file index)."""
    FL = sorted(files, key=lambda f: (f['mir'].get('start') or 0, f['path']))
    spread = _spread(FL)

    def dist(a, b, job):
        sp = spread.get(job, {})
        z = [abs(a[k] - b[k]) / sp[k] for k in a if k in b and k in sp]
        return st.median(z) if len(z) >= 5 else None

    units = []
    for fi, f in enumerate(FL):
        runs = f['runs']; i = 0
        ins = temperature_of(f, profile); tags = tags_of(f, profile)
        while i < len(runs):
            j = i
            while j + 1 < len(runs) and runs[j + 1]['sn'] == runs[i]['sn']: j += 1
            grp = runs[i:j + 1]; last = grp[-1]
            u = Unit(); u.dev = device_of(f, profile); u.ins = ins; u.file = f; u.fi = fi
            u.stage = ins + (' PostBake' if 'POST-BAKE' in tags else '')
            u.idx, u.pid, u.runs, u.run_list = len(units), last['sn'], len(grp), grp
            u.res = 'F' if last['failed'] else 'P'; u.hb, u.sb = last['hb'], last['sb']
            fr = grp[0].get('id_read')
            u.arr = 'noread' if fr is None else ('unburned' if fr == 0.0 else 'burned')
            u.arr_key = key7(fr) if u.arr == 'burned' else None
            w = next((g.get('word') for g in reversed(grp) if g.get('word')), None)
            u.word_key = key7(f32(w)) if w else (u.arr_key if u.arr == 'burned' else None)
            u.post = next((g['post'] for g in reversed(grp) if g.get('post')), {})
            u.job = f['mir'].get('job'); u.pp = None; u.cls = ''; u.link = ''; u.link_unit = None
            u.amb = 0; u.dist = None; u.tags = tags; u.first_fail = last.get('first_fail', '')
            units.append(u); i = j + 1
    role = {}
    for fi, f in enumerate(FL):
        U = [u for u in units if u.fi == fi]
        nb = sum(u.arr == 'burned' for u in U)
        role[fi] = 'retest' if ('RETEST-name' in tags_of(f, profile) or (U and U[0].ins == 'Room' and nb > 0.2 * len(U))) else 'fresh'

    same_d = profile.get('same_part_distance', {}); diff_d = profile.get('different_part_distance', {})
    pp_n = 0
    by_word = collections.defaultdict(list)
    failpool = collections.defaultdict(list)
    used = collections.defaultdict(set)
    burned_pp, pp_last = {}, {}
    for u in units:
        if u.arr == 'burned':
            latest = {}
            for c in by_word.get(u.arr_key, []):
                if (c.fi, c.idx) < (u.fi, u.idx): latest[c.pp] = c
            cands = list(latest.values())
            same = [c for c in cands if pp_last[c.pp].stage == u.stage]
            for c in same:
                c._d = dist(u.post, c.post, u.job) if (u.post and c.post) else None
            SAME = same_d.get(u.ins, 0.1); DIFF = diff_d.get(u.ins, 99.0)
            fails = [c for c in same if pp_last[c.pp].res == 'F']
            passes = [c for c in same if pp_last[c.pp].res == 'P']
            best = None
            if same:
                ds = [c for c in same if c._d is not None]
                closest = min(ds, key=lambda c: c._d) if ds else None
                if passes and closest is not None and pp_last[closest.pp].res == 'P' and closest._d <= SAME and \
                        not any(c._d is not None and c._d <= SAME for c in fails):
                    best = closest; u.cls = 'CONFIRMED: passed part tested again at the same stage (measurements match)'
                elif fails:
                    fd = [c for c in fails if c._d is not None]
                    best = min(fd, key=lambda c: c._d) if fd else max(fails, key=lambda c: (c.fi, c.idx))
                    u.cls = 'retest of earlier fail'
                elif passes:
                    pd = [c for c in passes if c._d is not None]
                    cl = min(pd, key=lambda c: c._d) if pd else None
                    if cl is not None and cl._d >= DIFF:
                        u.cls = 'same ID word as an earlier pass, measurements differ -> different part'
                    else:
                        pm = cl or passes[-1]
                        u.cls = 'POSSIBLE: may be a passed part tested again (%s) - check' % (
                            'measurements in grey zone' if cl is not None else 'no measurements to compare')
                        u.link = 'possible match: ' + pm.ref(); u.dist = cl._d if cl is not None else None
                u.amb = len(same)
                if best is not None: u.dist = best._d
            if best is None:
                prev = [c for c in cands if pp_last[c.pp].stage != u.stage]
                if prev:
                    best = max(prev, key=lambda c: (pp_last[c.pp].res == 'P', c.fi, c.idx))
                    if pp_last[best.pp].res == 'F' and not u.cls:
                        u.cls = 'CHECK: first test at this stage, but its earlier result (%s) was FAIL' % best.stage
                    u.cls = u.cls or 'first test at this stage (came from %s)' % best.stage
                    u.amb = max(u.amb, len(prev))
            if best is None:
                pp_n += 1; u.pp = pp_n
                u.cls = u.cls or 'programmed on arrival but not seen earlier in these files'
            else:
                u.pp = best.pp; u.link_unit = best
                u.link = (u.link + ' | linked to ' if u.link else '') + best.ref()
        else:
            cands = [pp for pp in failpool[u.stage] if pp not in used[u.fi] and (u.arr == 'noread' or not burned_pp.get(pp))]
            if role[u.fi] == 'retest' and cands:
                u.pp = cands[0]; u.cls = 'anonymous retest (assumed = one earlier fail at this stage)'
            elif role[u.fi] == 'retest' and u.ins == 'Room' and not u.stage.endswith('PostBake'):
                pp_n += 1; u.pp = pp_n
                u.cls = 'EXTRA: more parts in this retest file than earlier fails left - likely the same part re-inserted under a new Part ID'
            elif u.ins != 'Room' or u.stage.endswith('PostBake'):
                pp_n += 1; u.pp = -pp_n
                u.cls = 'anonymous at %s (ID not read - contact/early fail); one of the parts already in the lot' % u.stage
            else:
                pp_n += 1; u.pp = pp_n; u.cls = 'new part (fresh file)'
        used[u.fi].add(u.pp)
        pp_last[u.pp] = u
        if u.word_key:
            by_word[u.word_key].append(u); burned_pp[u.pp] = True
        if u.res == 'F' and u.pp not in failpool[u.stage]: failpool[u.stage].append(u.pp)
        if u.res == 'P' and u.pp in failpool[u.stage]: failpool[u.stage].remove(u.pp)
    return FL, units, role


def kind_of(u):
    c = u.cls
    for k, lab in (('CONFIRMED', 'passed part tested again'), ('POSSIBLE', 'possible repeat'), ('EXTRA', 'extra part in retest'),
                   ('CHECK', 'failed earlier stage'), ('programmed on arrival', 'not seen earlier'),
                   ('retest of earlier fail', 'retest of earlier fail'), ('anonymous retest', 'retest of earlier fail'),
                   ('first test at this stage', 'first time at this stage'), ('new part', 'first time at this stage'),
                   ('same ID word', 'first time at this stage'), ('anonymous at', 'ID not read')):
        if c.startswith(k): return lab
    return c[:30]


def stage_totals(FL, units):
    stages = []
    for u in units:
        if u.stage not in stages: stages.append(u.stage)
    out = []
    for stg in stages:
        U = [u for u in units if u.stage == stg]
        fis = sorted({u.fi for u in U})
        last = {}
        for u in U: last[u.pp] = u
        ident = [pp for pp in last if pp > 0]
        good = sum(1 for pp in ident if last[pp].res == 'P')
        poss_p = sum(1 for u in U if u.cls.startswith('POSSIBLE') and last.get(u.pp) is u and u.res == 'P')
        out.append(dict(stage=stg, files=len(fis), runs=sum(len(FL[i]['runs']) for i in fis), units=len(U),
                        pass_runs=sum(1 for i in fis for r in FL[i]['runs'] if not r['failed']),
                        parts=len(ident), extra=sum(1 for u in U if u.cls.startswith('EXTRA')),
                        good_max=good, good_min=good - poss_p,
                        confirmed=sum(1 for u in U if u.cls.startswith('CONFIRMED')),
                        possible=sum(1 for u in U if u.cls.startswith('POSSIBLE')),
                        unseen=sum(1 for u in U if u.cls.startswith('programmed on arrival')),
                        anon_rt=sum(1 for u in U if u.cls.startswith('anonymous retest')),
                        check=sum(1 for u in U if u.cls.startswith('CHECK'))))
    return out
