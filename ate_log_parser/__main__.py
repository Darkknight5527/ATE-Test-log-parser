"""Command line:  python -m ate_log_parser <files/folders> [-o report.xlsx] [--profile my_device.json]
                                         [--traveller traveller.xlsx] [--stats pass|all]"""
import argparse
from .profile import load
from .report import build


def main():
    ap = argparse.ArgumentParser(prog='ate_log_parser', description='STDF files of one lot -> lot report (+ traveller fill-in)')
    ap.add_argument('inputs', nargs='+', help='STDF files and/or folders (searched recursively)')
    ap.add_argument('-o', '--output', help='report .xlsx (default: next to the first STDF)')
    ap.add_argument('--profile', help='device profile JSON (which tests identify a part); default: generic')
    ap.add_argument('--traveller', help='lot traveller .xlsx: a copy is written with an "STDF Fill-in" sheet added')
    ap.add_argument('--traveller-out', help='where to write the traveller copy')
    ap.add_argument('--stats', choices=['pass', 'all'], default='pass', help='statistics on PASS units (viewer default) or ALL units')
    a = ap.parse_args()
    build(a.inputs, a.output, load(a.profile), a.traveller, a.traveller_out, a.stats)


if __name__ == '__main__':
    main()
