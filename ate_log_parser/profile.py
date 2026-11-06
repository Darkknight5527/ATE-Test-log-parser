"""Device profiles: everything product-specific lives here, NOT in the code.

A profile is a small JSON file. The generic default works for any STDF (no part identification);
a product profile (kept on the user's PC, never committed) tells the tool which tests identify a part.

Keys
  name                  : display name
  readback_test         : part of the test name whose value is the ID read back from the part
                          (0 = not yet programmed, missing = failed before it was read)
  blown_bit_regex       : regex on test names that carry the programmed ID bit by bit;
                          named groups: 'bit' (bit number) and optional 'un' (set when the bit is NOT programmed)
  compare_suites        : test suites (text before ':') whose values are compared to tell parts apart
                          when two parts carry the same ID word
  device_regex          : regex on the program (job) name; group 1 = device (files of different devices are never mixed)
  temperature_rules     : list of [stage, regex on 'job + file name'] checked in order; default stage = Room
  excluded_tags         : words in the file name / lot that mark set-up files (listed, not counted as lot tests)
  bake_regex            : regex marking post-bake files (own stage)
  retest_regex          : regex marking retest files by name
  same_part_distance    : {stage: max distance to call two measurements the same part}
  different_part_distance: {stage: min distance to call them different parts}
  continuity_suite      : suite name prefix counted as FailConti in the per-file sheets
  traveller_sheets      : {stage: [initial sheet, retest sheets...]} - lot traveller sheet names
"""
import json, os

DEFAULT = {
    'name': 'Generic (no part ID)',
    'readback_test': '',
    'blown_bit_regex': '',
    'compare_suites': [],
    'device_regex': '',
    'temperature_rules': [['Cold', r'COLD'], ['Hot', r'HOT'], ['Room', r'ROOM']],
    'excluded_tags': ['SANITY', 'FIFO', 'ENG', 'ESD', 'SAMPLE'],
    'bake_regex': r'BAKE',
    'retest_regex': r'RETEST|FAILTEST|FAIL_TEST|RE_TEST',
    'same_part_distance': {'Room': 0.15, 'Hot': 0.08, 'Cold': 0.08},
    'different_part_distance': {'Room': 0.35, 'Hot': 99.0, 'Cold': 99.0},
    'continuity_suite': 'CONTINUITY',
    'traveller_sheets': {'Room': ['T1', 'T2', 'T3'], 'Hot': ['HOT_T1', 'HOT_T2'], 'Cold': ['COLD_T1', 'COLD_T2']},
}


def profiles_dir():
    base = os.environ.get('APPDATA') or os.path.join(os.path.expanduser('~'), '.config')
    return os.path.join(base, 'ate_log_parser', 'profiles')


def list_profiles():
    d = profiles_dir()
    out = {'Generic (no part ID)': None}
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f.lower().endswith('.json'):
                try:
                    p = load(os.path.join(d, f)); out[p.get('name', f)] = os.path.join(d, f)
                except Exception:
                    pass
    return out


def load(path=None):
    prof = json.loads(json.dumps(DEFAULT))
    if path:
        with open(path, encoding='utf-8') as fh:
            user = json.load(fh)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(prof.get(k), dict):
                prof[k].update(v)
            else:
                prof[k] = v
    return prof


def install(src_path):
    """Copy a profile JSON into the user's profile folder; returns its name."""
    p = load(src_path)
    os.makedirs(profiles_dir(), exist_ok=True)
    dst = os.path.join(profiles_dir(), os.path.basename(src_path))
    with open(src_path, encoding='utf-8') as a, open(dst, 'w', encoding='utf-8') as b:
        b.write(a.read())
    return p.get('name', os.path.basename(src_path))
