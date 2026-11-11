"""Print the test limits stored in STDF datalogs.

Usage:
  python get_limits.py <file or folder> [test number or part of name]

Examples:
  python get_limits.py C:\\Data\\MyLot 1000
  python get_limits.py C:\\Data\\MyLot\\cold.stdf VREF
  python get_limits.py C:\\Data\\MyLot\\cold.stdf          (all tests)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stdf_lot_yield_summary as S


def fmt(v):
    if v is None:
        return "-"
    return f"{v:.6g}"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    target = sys.argv[1]
    want = sys.argv[2] if len(sys.argv) > 2 else None
    files = [target] if os.path.isfile(target) else S.collect_stdf_files(target)
    for f in files:
        s = S.read_stdf(f, S.Options(), keep_values=True)
        print(f"\n{os.path.basename(f)}")
        print(f"  Area: {S.detect_temperature(s)}   Program: {s.job_name or '-'}")
        found = False
        for tn in sorted(s.tests):
            name, units, lo, hi = s.tests[tn][:4]
            if want and not (want == str(tn) or want.lower() in (name or "").lower()):
                continue
            found = True
            print(f"  {tn:<10} {name}  [{units or '-'}]  Low {fmt(lo)}  High {fmt(hi)}")
        if not found:
            print("  (test not in this file)")


if __name__ == "__main__":
    main()
