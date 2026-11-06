#!/usr/bin/env python3
"""
check_repo_clean.py - stop tester / company data from being committed.

  python tools/check_repo_clean.py --staged   # files staged for commit (used by the pre-commit hook)
  python tools/check_repo_clean.py --all      # every tracked + untracked-not-ignored file
                                              # (run this once before the FIRST push)

Blocks a file if:
  1. its extension is a datalog / report type (.stdf .std .atdf .xlsx .csv ...), unless under demo/
  2. its content starts like an STDF file (FAR record), whatever its name
  3. it is larger than 2 MB
  4. it contains any word from .private_denylist.txt (case-insensitive)

.private_denylist.txt is a LOCAL file (git-ignored, never committed). Put one word per line:
company name, customer/device names, tester host names, lot prefixes, people's names, etc.
"""
import os, subprocess, sys

BLOCK_EXT = ('.stdf', '.std', '.stdf.gz', '.std.gz', '.atdf', '.xlsx', '.xlsm', '.xls', '.csv', '.tsv',
             '.pdf', '.docx', '.pptx', '.wat')
MAX_BYTES = 2 * 1024 * 1024
ROOT = subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True, text=True).stdout.strip() or '.'


def files(mode):
    if mode == '--staged':
        cmd = ['git', 'diff', '--cached', '--name-only', '--diff-filter=ACMR']
    else:
        cmd = ['git', 'ls-files', '--cached', '--others', '--exclude-standard']
    out = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT).stdout
    return [f for f in out.splitlines() if f.strip()]


def denylist():
    p = os.path.join(ROOT, '.private_denylist.txt')
    if not os.path.exists(p):
        return []
    with open(p, encoding='utf-8', errors='ignore') as fh:
        return [w.strip().lower() for w in fh if w.strip() and not w.startswith('#')]


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else '--staged'
    words = denylist()
    problems = []
    for f in files(mode):
        path = os.path.join(ROOT, f)
        if not os.path.isfile(path):
            continue
        low = f.lower().replace('\\', '/')
        in_demo = low.startswith('demo/') or '/demo/' in low
        if low.endswith(BLOCK_EXT) and not in_demo:
            problems.append((f, 'blocked file type')); continue
        size = os.path.getsize(path)
        if size > MAX_BYTES:
            problems.append((f, 'larger than 2 MB (%d bytes)' % size)); continue
        with open(path, 'rb') as fh:
            data = fh.read()
        if data[2:4] == b'\x00\x0a' and len(data) > 6 and not in_demo:
            problems.append((f, 'looks like an STDF file (FAR record at start)')); continue
        if words and f != '.private_denylist.txt':
            text = data.decode('utf-8', errors='ignore').lower()
            hits = sorted({w for w in words if w in text or w in low})
            if hits:
                problems.append((f, 'contains private word(s): ' + ', '.join(hits)))
    if not words:
        print('NOTE: .private_denylist.txt not found - name check skipped. Create it (see tools/check_repo_clean.py).')
    if problems:
        print('\nCOMMIT BLOCKED - possible company / tester data:\n')
        for f, why in problems:
            print('  %-60s %s' % (f, why))
        print('\nRemove these from the commit (git restore --staged <file>) or clean them, then try again.')
        sys.exit(1)
    print('Repo check OK (%d file(s) checked).' % len(files(mode)))


if __name__ == '__main__':
    main()
