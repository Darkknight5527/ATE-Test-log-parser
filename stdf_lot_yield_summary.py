#!/usr/bin/env python3
"""
STDF Multi-Stage Lot Yield Analyzer  (v2.2 - counts physical parts, not test runs)

Based on the original stdf_lot_yield_summary.py. Same report layout, colours and CLI, with these changes:
  1. Attach STDF files (or folders) directly:  -i file1.stdf file2.stdf folder ...
     Fresh Run / Retest 1 / Retest 2 ... and Room / Hot / Cold are worked out automatically from the data.
     (--stage "NAME" PATH still works if you want to force a stage by hand.)
  2. Counts PHYSICAL PARTS, not PRR records:
       - a part retested inside the same file (same Part ID on following rows) counts once, last result wins;
       - parts are followed from file to file with the eFuse data:
           * exact fuse word  = BLOWN bits of the post-burn eFuse resistance tests,
           * fuse read-back   = the fingerprint test (name set in the device profile),
           * post-trim values = tie-break when two parts share the same fuse word;
       - parts that failed before the fuse was burned carry no identity; they are counted as a pool
         ("unidentified fails") and every retest is checked against that pool.
  3. Room, Hot and Cold each get their own waterfall; the final lot yield counts a part as good only if its
     last result was PASS at every temperature it was tested at.
  4. Leaves out: identical duplicate files, empty files, and files whose name matches --exclude
     (default FIFO / BOARD), and every sanity-check file ("Sanity check" in any spelling, in the file
     name, lot ID or program name).
  5. Extra sheets after the original ones: Files, Part Ledger, Exceptions. Yields are real numbers (% format).

Usage:
  python stdf_lot_yield_summary.py -i "C:\\...\\lot_1153" -o "Lot_Yield_Report.xlsx"
  python stdf_lot_yield_summary.py -i a.stdf b.stdf c.stdf -o out.xlsx
  python stdf_lot_yield_summary.py --stage "Fresh Run" "C:\\...\\Firstrun_Data" --stage "Retest 1" "C:\\...\\Retest" -o out.xlsx
"""

import os
import sys
import glob
import gzip
import re
import struct
import argparse
import hashlib
import datetime
import statistics
from collections import Counter, OrderedDict, defaultdict
from typing import Dict, List, Optional, Tuple

VERSION = "2.6"

# ----------------------------------------------------------------------------------------------------------
# Settings (all overridable from the command line)
# ----------------------------------------------------------------------------------------------------------
# Product-specific names are NOT set here: they come from a device profile (profiles.py; JSON file on the user's PC).
# Without a profile, parts are followed by Part ID inside a file only.
DEFAULT_FINGERPRINT = ""                          # profile readback_test: fuse read-back test (value = fuse word as a float)
DEFAULT_FUSE_BITS = ""                            # profile fuse_bits_prefix: per-bit fuse resistance tests
ABORT_HB = 2                                       # hard bin used for aborted runs (HB 65535)
DEFAULT_EXCLUDE = r"FIFO|BOARD"                    # file names to leave out (besides sanity checks)
SANITY_RX = re.compile(r"S[\W_]*A[\W_]*N[\W_]*I[\W_]*T[\W_]*Y|SAN[\W_]*CHK", re.I)   # "Sanity check" in any form
BLOWN_OHMS = 1000.0                               # resistance above this = blown bit
BIT_RX = None                                      # profile fuse_bit_regex: group 'bit' (+ optional 'bank')
FUSE_BANK = ""                                     # only this bank's bits ('' = all)
BURN_FAIL_KEYS = ()                                # soft-bin name words = failed while burning the fuse
BURN_CHECK_KEYS = ()                               # soft-bin name words that are the check BEFORE burning
DEVICE_RX = None                                   # device version from program / file name
LOT_RX, LOT_PREFIX = None, ""                      # lot number from lot ID / part type / folder
HOT_RX = COLD_RX = POSTBAKE_OVERRIDE_RX = None     # optional overrides of the insertion detection
# post-trim test suites used to tell apart two parts with the same fuse word (same temperature only)
POST_TRIM_PREFIXES = ()     # profile match_tests: post-trim tests that tell apart parts sharing a fuse word
SAME_PART_MAX_DIFF = 0.003     # median relative difference of post-trim values: below = same part
DIFF_PART_MIN_DIFF = 0.010     # above = certainly a different part
MIN_COMMON_TESTS = 20

RETEST_NO_RX = re.compile(r"(?:RE[-_]?TEST|FAIL[-_]?TEST)[-_]?(\d)(?!\d)", re.I)
RETEST_NAME_RX = re.compile(r"RE[-_]?TEST|FAIL[-_]?TEST|FAILS?_RETEST|FAIL_RETEST", re.I)
TEMPS = ("Room", "PostBake", "Hot", "Cold")          # test insertions, in flow order
TEMP_LABEL = {"Room": "Room 25C", "PostBake": "Post-Bake 25C", "Hot": "Hot 125C", "Cold": "Cold -55C"}
POSTBAKE_RX = re.compile(r"POST[-_ ]?BAKE", re.I)


# ----------------------------------------------------------------------------------------------------------
# STDF reading
# ----------------------------------------------------------------------------------------------------------
def open_stdf(file_path: str):
    """Open standard or gzip-compressed STDF file."""
    with open(file_path, "rb") as test_f:
        magic = test_f.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(file_path, "rb")
    return open(file_path, "rb")


def read_pstring(data: bytes, offset: int) -> Tuple[str, int]:
    """Read STDF Pascal-style string (1-byte length prefix)."""
    if offset >= len(data):
        return "", offset
    length = data[offset]
    offset += 1
    val = data[offset: offset + length].decode("latin1", errors="ignore")
    return val, offset + length


def f32(x) -> float:
    """Round a number the way the tester stores it (32-bit float)."""
    return struct.unpack("<f", struct.pack("<f", float(x)))[0]


class Run:
    """One PRR record = one insertion of a part into the socket."""
    __slots__ = ("file", "row", "pid", "flg", "hb", "sb", "site", "read", "word", "post", "vals", "fails", "tt", "nt")

    def __init__(self):
        self.read = None      # fingerprint (fuse read-back) value, None if test not reached
        self.word = None      # exact fuse word from BLOWN bits, None if post-burn test not reached
        self.post = {}        # post-trim values
        self.vals = None      # {test number: value} - only when the file is read with keep_values
        self.fails = None     # set of failing test numbers - only with keep_values
        self.tt = 0           # test time (ms)
        self.nt = 0           # number of tests executed


class STDFFileSummary:
    """Parses an individual STDF file."""
    def __init__(self, file_path: str, fingerprint: Optional[str] = None, fuse_bits: Optional[str] = None):
        self.file_path = file_path
        self.file_name = os.path.basename(file_path)
        self.fingerprint = DEFAULT_FINGERPRINT if fingerprint is None else fingerprint
        self.fuse_bits = DEFAULT_FUSE_BITS if fuse_bits is None else fuse_bits
        self.lot_id = ""
        self.part_type = ""
        self.job_name = ""
        self.temp = ""
        self.start_t = 0
        self.endian = "<"
        self.md5 = ""

        self.keep_values = False
        self.tests: Dict[int, list] = {}      # test number -> [name, units, lo, hi, scale] (keep_values)
        self.finish_t = 0
        self.node = ""
        self.tester = ""
        self.hbr_defs: Dict[int, Dict[str, str]] = {}
        self.sbr_defs: Dict[int, Dict[str, str]] = {}
        self.runs: List[Run] = []
        self.truncated = ""

        # set later
        self.temperature = "Room"
        self.stage = ""
        self.stage_reason = ""
        self.status = "Used"
        self.units: List["Unit"] = []

    # counts kept for compatibility with the original report (per test run)
    @property
    def total_parts(self):
        return len(self.runs)

    @property
    def pass_parts(self):
        return sum(1 for r in self.runs if r.hb == 1)

    @property
    def fail_parts(self):
        return self.total_parts - self.pass_parts

    def parse(self):
        h = hashlib.md5()
        with open_stdf(self.file_path) as f:
            raw = f.read()
        h.update(raw)
        self.md5 = h.hexdigest()
        if len(raw) < 6:
            return
        if raw[2] == 0 and raw[3] == 10:
            self.endian = ">" if raw[4] == 1 else "<"
        e = self.endian
        u2 = struct.Struct(e + "H")
        names: Dict[int, str] = {}
        cur: Dict[int, Run] = {}
        keep = self.keep_values
        tests = self.tests
        bits_key = self.fuse_bits
        fp_key = self.fingerprint
        bit_rx, bank, post_prefixes, blown = BIT_RX, FUSE_BANK, POST_TRIM_PREFIXES, BLOWN_OHMS
        pos, L = 0, len(raw)
        while pos + 4 <= L:
            rec_len = u2.unpack_from(raw, pos)[0]
            rec_typ, rec_sub = raw[pos + 2], raw[pos + 3]
            data = raw[pos + 4: pos + 4 + rec_len]
            pos += 4 + rec_len
            if len(data) < rec_len:
                self.truncated = f"file ends in the middle of a record (type {rec_typ}/{rec_sub}, " \
                                 f"needs {rec_len} bytes, {len(data)} left)"
                break

            if rec_typ == 15 and rec_sub == 10 and len(data) >= 12:          # PTR
                tn, head, site = struct.unpack_from(e + "IBB", data, 0)
                val = struct.unpack_from(e + "f", data, 8)[0]
                txt, off = read_pstring(data, 12)
                if txt:
                    names.setdefault(tn, txt)
                else:
                    txt = names.get(tn, "")
                run = cur.get((head, site))
                if run is None:
                    run = cur[(head, site)] = Run()
                if keep:
                    td = tests.get(tn)
                    if td is None or (td[4] is None and len(data) > off + 2):
                        td = self._ptr_def(data, off, txt, td)
                        tests[tn] = td
                    if run.vals is None:
                        run.vals, run.fails = {}, set()
                    tflg = data[6]
                    if not (tflg & 0x02) and val == val:          # bit1 = result not valid
                        run.vals[tn] = val * td[5]
                    if tflg & 0x80:
                        run.fails.add(tn)
                if fp_key and fp_key in txt and run.read is None:
                    # a real read-back is a 48-bit fuse word: whole number, 0 .. 2^48. Anything else is a failed read.
                    run.read = val if (val == val and 0 <= val < 2.0 ** 48 and val == int(val)) else None
                elif bits_key and bit_rx is not None and txt.startswith(bits_key):
                    m = bit_rx.search(txt)
                    if m and (not bank or m.groupdict().get("bank") == bank):
                        if run.word is None:
                            run.word = 0
                        if val > blown:
                            run.word |= 1 << int(m.group("bit"))
                elif post_prefixes and txt.startswith(post_prefixes):
                    run.post[txt] = val

            elif rec_typ == 5 and rec_sub == 10:                                # PIR
                if len(data) >= 2:
                    cur[(data[0], data[1])] = Run()

            elif rec_typ == 5 and rec_sub == 20 and len(data) >= 9:            # PRR
                head, site, part_flg, _nt, hard_bin, soft_bin = struct.unpack_from(e + "BBBHHH", data, 0)
                pid, _ = read_pstring(data, 17) if len(data) > 17 else ("", 0)
                run = cur.pop((head, site), None) or Run()
                run.nt = _nt
                if len(data) >= 17:
                    run.tt = struct.unpack_from(e + "I", data, 13)[0]
                run.file, run.row, run.pid, run.flg = self, len(self.runs) + 1, pid, part_flg
                run.hb, run.sb, run.site = hard_bin, soft_bin, site
                if run.word == 0:
                    run.word = None
                self.runs.append(run)

            elif rec_typ == 1 and rec_sub == 10 and len(data) >= 15:           # MIR
                self.start_t = struct.unpack_from(e + "I", data, 4)[0]
                offset = 15
                self.lot_id, offset = read_pstring(data, offset)
                self.part_type, offset = read_pstring(data, offset)
                self.node, offset = read_pstring(data, offset)
                self.tester, offset = read_pstring(data, offset)
                self.job_name, offset = read_pstring(data, offset)
                for _ in range(6):
                    _x, offset = read_pstring(data, offset)
                self.temp, offset = read_pstring(data, offset)

            elif rec_typ == 1 and rec_sub == 20 and len(data) >= 4:            # MRR
                self.finish_t = struct.unpack_from(e + "I", data, 0)[0]

            elif rec_typ == 1 and rec_sub in (40, 50) and len(data) >= 8:      # HBR / SBR
                _h, _s, b, _cnt = struct.unpack_from(e + "BBHI", data, 0)
                pf = chr(data[8]) if len(data) > 8 else ("P" if b == 1 else "F")
                name, _ = read_pstring(data, 9) if len(data) > 9 else ("", 0)
                defs = self.hbr_defs if rec_sub == 40 else self.sbr_defs
                if b not in defs or (name and not defs[b]["name"]):
                    defs[b] = {"name": name if name else ("PASS" if b == 1 else ""), "pf": pf}

    _PREFIX = {15: "f", 12: "p", 9: "n", 6: "u", 3: "m", 0: "", -3: "k", -6: "M", -9: "G"}

    def _ptr_def(self, data, off, txt, old):
        """Limits / units of a parametric test (from its first full PTR). [name, units, lo, hi, scale_exp, factor]"""
        e = self.endian
        name, units, lo, hi, sc = txt, "", None, None, None
        try:
            _alarm, off = read_pstring(data, off)
            if len(data) >= off + 12:
                opt = data[off]
                res_scal = struct.unpack_from(e + "b", data, off + 1)[0]
                lo_v, hi_v = struct.unpack_from(e + "ff", data, off + 4)
                units, _ = read_pstring(data, off + 12)
                sc = res_scal if res_scal in self._PREFIX and units else 0
                f = 10.0 ** sc
                lo = None if opt & 0x50 else lo_v * f            # bit4 invalid / bit6 no low limit
                hi = None if opt & 0xA0 else hi_v * f            # bit5 invalid / bit7 no high limit
                units = (self._PREFIX.get(sc, "") + units) if units else ""
        except Exception:  # noqa
            pass
        if old is not None and sc is None:
            return old
        return [name, units, lo, hi, sc, 10.0 ** (sc or 0)]

    @property
    def finish_str(self):
        if not self.finish_t:
            return ""
        return datetime.datetime.fromtimestamp(self.finish_t, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def parse_header(self):
        """Read only the file header (MIR): lot, part type, program, start time. Fast - used to list files."""
        with open_stdf(self.file_path) as f:
            raw = f.read(65536)
        if len(raw) >= 6 and raw[2] == 0 and raw[3] == 10:
            self.endian = ">" if raw[4] == 1 else "<"
        e = self.endian
        pos = 0
        while pos + 4 <= len(raw):
            rec_len = struct.unpack_from(e + "H", raw, pos)[0]
            typ, sub = raw[pos + 2], raw[pos + 3]
            data = raw[pos + 4: pos + 4 + rec_len]
            pos += 4 + rec_len
            if typ == 1 and sub == 10 and len(data) >= 15:
                self.start_t = struct.unpack_from(e + "I", data, 4)[0]
                off = 15
                self.lot_id, off = read_pstring(data, off)
                self.part_type, off = read_pstring(data, off)
                _n, off = read_pstring(data, off)
                _t, off = read_pstring(data, off)
                self.job_name, off = read_pstring(data, off)
                return True
            if typ in (5, 15):       # parts already started: no MIR
                break
        return False

    @property
    def start_str(self):
        if not self.start_t:
            return ""
        return datetime.datetime.fromtimestamp(self.start_t, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")


# ----------------------------------------------------------------------------------------------------------
# Units (one part inside one file) and physical parts (one part across files)
# ----------------------------------------------------------------------------------------------------------
def runs_same_part(a: Run, b: Run) -> Optional[bool]:
    """True/False if the eFuse data proves same/different part, None if it can't tell."""
    if a.word is not None and b.word is not None:
        return a.word == b.word
    if a.read and b.read:
        return a.read == b.read
    return None


class Unit:
    """One physical part inside one file (a part retested on following rows of the same file = one unit)."""

    def __init__(self, f: STDFFileSummary, runs: List[Run]):
        self.file = f
        self.runs = runs
        self.part: Optional["Part"] = None
        self.note = ""

    @property
    def pid(self):
        return self.runs[0].pid

    @property
    def last(self) -> Run:
        return self.runs[-1]

    @property
    def passed(self):
        return self.last.hb == 1

    @property
    def word(self):
        for r in reversed(self.runs):
            if r.word is not None:
                return r.word
        return None

    @property
    def read(self):
        for r in reversed(self.runs):
            if r.read:
                return r.read
        return None

    @property
    def fused_at_start(self) -> Optional[bool]:
        """Was the part already burned when it entered this file? None if the fuse read was never reached."""
        r = self.runs[0]
        if r.read is None:
            return None
        return r.read != 0

    @property
    def post(self):
        for r in reversed(self.runs):
            if len(r.post) >= MIN_COMMON_TESTS:
                return r.post
        return self.last.post

    @property
    def fingerprint(self):
        """(kind, value) used to look the part up: exact word, else fuse read-back (if already fused)."""
        if self.word is not None:
            return "word", self.word
        if self.read:
            return "read", self.read
        return None


def build_units(f: STDFFileSummary, exc) -> List[Unit]:
    """Collapse runs of the same part inside a file."""
    units: List[Unit] = []
    open_by_pid: Dict[str, Unit] = {}
    aborted = [r for r in f.runs if r.hb == 65535]
    if aborted:
        # HARD_BIN 65535 = no bin assigned (run aborted by the test software, not a device result).
        # Lot rule: reported as hard bin 2 (a fail) like on the lot traveller.
        for r in aborted:
            r.hb = ABORT_HB
        exc.add("Warning", "Aborted insertion (no bin) - reported as Bin 2", f.file_name,
                "; ".join(f"PID {r.pid} (row {r.row})" for r in aborted), "")
    for run in f.runs:
        u = open_by_pid.get(run.pid) if run.pid else None
        if u is not None:
            same = runs_same_part(u.last, run)
            adjacent = units and units[-1] is u
            if same is False:
                exc.add("Warning", "Part ID reused for a different part", f.file_name,
                        f"PID {run.pid} (rows {u.last.row} and {run.row})",
                        "Same Part ID but different eFuse data: counted as two different parts.")
                u = None
            elif not adjacent and same is None and not (run.flg & 1):
                exc.add("Info", "Part ID repeated in file", f.file_name,
                        f"PID {run.pid} (rows {u.runs[0].row} and {run.row})",
                        "Treated as the same part tested again (no eFuse data to check).")
        if u is None:
            u = Unit(f, [run])
            units.append(u)
            if run.pid:
                open_by_pid[run.pid] = u
        else:
            u.runs.append(run)
    return units


def pid_key(pid: str):
    """Part ID as a comparable key ('0900' and '900' are the same serial)."""
    s = (pid or "").strip()
    return s.lstrip("0") or s


TEMP_PREFIX = {"Room": "R", "PostBake": "PB", "Hot": "H", "Cold": "C"}


class Part:
    """One physical part at one temperature. label = R1, R2 ... at room, H1, H2 ... at hot, C1 ... at cold."""
    _n = 0
    _per_temp = Counter()

    def __init__(self, temperature: str, how: str):
        Part._n += 1
        self.no = Part._n
        Part._per_temp[temperature] += 1
        self.label = f"{TEMP_PREFIX[temperature]}{Part._per_temp[temperature]}"
        self.temperature = temperature
        self.units: List[Unit] = []
        self.word = None
        self.read = None
        self.post: Dict[str, float] = {}
        self.how = how                      # how the part was identified
        self.room_part: Optional["Part"] = None
        self.later: Dict[str, "Part"] = {}  # temperature -> linked part at hot/cold
        self.anonymous = False

    def add(self, u: Unit):
        self.units.append(u)
        u.part = self
        if u.word is not None:
            self.word = u.word
        if u.read:
            self.read = u.read
        if len(u.post) >= MIN_COMMON_TESTS:
            self.post = u.post

    @property
    def passed(self):
        return self.units[-1].passed if self.units else False

    @property
    def first_passed(self):
        return self.units[0].passed if self.units else False

    def fp_matches(self, kind, val) -> bool:
        if kind == "word":
            if self.word is not None:
                return self.word == val
            return self.read is not None and self.read == f32(val)
        # kind == read
        if self.word is not None:
            return f32(self.word) == val
        return self.read is not None and self.read == val


def post_diff(a: Dict[str, float], b: Dict[str, float]) -> Optional[float]:
    keys = [k for k in a if k in b]
    if len(keys) < MIN_COMMON_TESTS:
        return None
    return statistics.median(abs(a[k] - b[k]) / max(abs(a[k]), abs(b[k]), 1e-12) for k in keys)


# ----------------------------------------------------------------------------------------------------------
# Exceptions log
# ----------------------------------------------------------------------------------------------------------
class Exceptions:
    def __init__(self):
        self.rows = []

    def add(self, severity, kind, file_name, parts, details):
        self.rows.append((severity, kind, file_name, parts, details))


# ----------------------------------------------------------------------------------------------------------
# Stage groups
# ----------------------------------------------------------------------------------------------------------
class StageGroup:
    """Aggregates a test stage group of STDF files (Fresh Run, Retest 1, Retest 2, etc.)."""

    def __init__(self, name: str, file_paths: List[str] = None, is_fresh: bool = False, temperature: str = "Room"):
        self.name = name
        self.file_paths = file_paths or []
        self.is_fresh = is_fresh
        self.temperature = temperature
        self.files: List[STDFFileSummary] = []

        self.hbin_names: Dict[int, str] = {1: "PASS"}
        self.sbin_names: Dict[int, str] = {1: "PASS", 65535: "ABORTED - NO BIN (reported as HB 2)"}
        self.lot_ids = set()
        self.part_types = set()
        self.job_names = set()

        # filled by the tracker
        self.cum_good = 0
        self.population = 0
        self.recovered = 0

    show_temp = True   # set to False when the lot was tested at one temperature only (names then match v1)

    @property
    def display_name(self):
        return f"{self.name} ({TEMP_LABEL[self.temperature]})" if self.show_temp else self.name

    @property
    def units(self) -> List[Unit]:
        return [u for f in self.files for u in f.units]

    def stage_parts(self) -> List[Unit]:
        """One entry per physical part tested in this stage: its LAST insertion in the stage."""
        last: "OrderedDict[int, Unit]" = OrderedDict()
        for u in self.units:
            key = u.part.no if u.part is not None else -id(u)
            last.pop(key, None)
            last[key] = u
        return list(last.values())

    @property
    def total_parts(self):
        return len(self.stage_parts())

    @property
    def pass_parts(self):
        return sum(1 for u in self.stage_parts() if u.passed)

    @property
    def fail_parts(self):
        return self.total_parts - self.pass_parts

    @property
    def total_runs(self):
        return sum(len(f.runs) for f in self.files)

    @property
    def yield_pct(self) -> float:
        return (self.pass_parts / self.total_parts * 100) if self.total_parts > 0 else 0.0

    @property
    def combined_hbins(self):
        return Counter(u.last.hb for u in self.stage_parts())

    @property
    def combined_sbins(self):
        return Counter(u.last.sb for u in self.stage_parts())

    def collect_names(self):
        for s in self.files:
            if s.lot_id:
                self.lot_ids.add(s.lot_id)
            if s.part_type:
                self.part_types.add(s.part_type)
            if s.job_name:
                self.job_names.add(s.job_name)
            for defs, names in ((s.hbr_defs, self.hbin_names), (s.sbr_defs, self.sbin_names)):
                for b, info in defs.items():
                    if b not in names or (info["name"] and not names[b]):
                        names[b] = info["name"] or ("PASS" if b == 1 else "")


# ----------------------------------------------------------------------------------------------------------
# File classification
# ----------------------------------------------------------------------------------------------------------
def detect_temperature(f: STDFFileSummary) -> str:
    """Test insertion of a file: Room, PostBake (room test after bake), Hot or Cold."""
    if HOT_RX is not None and (HOT_RX.search(f.file_name) or HOT_RX.search(f.job_name)):
        return "Hot"
    if COLD_RX is not None and (COLD_RX.search(f.file_name) or COLD_RX.search(f.job_name)):
        return "Cold"
    if POSTBAKE_OVERRIDE_RX is not None and (POSTBAKE_OVERRIDE_RX.search(f.file_name) or POSTBAKE_OVERRIDE_RX.search(f.lot_id)):
        return "PostBake"
    job = f.job_name.lower()
    if "hot" in job:
        return "Hot"
    if "cold" in job:
        return "Cold"
    name = f.file_name.upper()
    if re.search(r"(^|[_\-])(HOT|HT)([_\-]|$)|_125_|_125C", name):
        return "Hot"
    if re.search(r"(^|[_\-])(COLD|CT|N55)([_\-]|$)|_-55_|_-55C", name):
        return "Cold"
    if POSTBAKE_RX.search(f.file_name) or POSTBAKE_RX.search(f.lot_id):
        return "PostBake"
    return "Room"


def device_variant(f: STDFFileSummary) -> str:
    if DEVICE_RX is None:
        return "?"
    m = DEVICE_RX.search(f.job_name) or DEVICE_RX.search(f.file_name)
    return (m.group(1) if m.groups() else m.group(0)) if m else "?"


def collect_stdf_files(path: str) -> List[str]:
    files = []
    if os.path.isdir(path):
        for root, _dirs, names in os.walk(path):
            for n in names:
                if n.lower().endswith((".stdf", ".stdf.gz", ".std")):
                    files.append(os.path.join(root, n))
    else:
        m = glob.glob(path)
        if m:
            files.extend(m)
        elif os.path.exists(path):
            files.append(path)
    return sorted(set(os.path.abspath(f) for f in files if os.path.isfile(f)))


# ----------------------------------------------------------------------------------------------------------
# The tracker: assigns stages and follows parts
# ----------------------------------------------------------------------------------------------------------
class LotTracker:
    def __init__(self, files: List[STDFFileSummary], forced: Dict[str, str], exc: Exceptions):
        self.files = files
        self.forced = forced          # file path -> stage name (from --stage)
        self.exc = exc
        self.stages: List[StageGroup] = []
        self.parts: Dict[str, List[Part]] = {t: [] for t in TEMPS}
        self.pool: Dict[str, int] = {t: 0 for t in TEMPS}          # unidentified failing parts
        self.pool_parts: Dict[str, List[Part]] = {t: [] for t in TEMPS}
        self.extra_as_new = False
        Part._n = 0                          # part numbering starts fresh for every analysis
        Part._per_temp = Counter()
        self._stage_parts: Dict[int, set] = {}
        self._rr = 0
        self._born_here: set = set()
        self._met_here: set = set()
        self._fresh_here = False
        self.order: List[str] = list(TEMPS)

    # ---- matching ------------------------------------------------------------------------------------
    def find(self, u: Unit, temp: str, strict: bool = False, avoid: Optional[set] = None) -> Tuple[Optional[Part], str]:
        """Look for an earlier part with the same eFuse data. Only parts that were ALREADY fused when they entered
        this file can be an earlier part (an unfused part was never burned before, so it has no history)."""
        fp = u.fingerprint
        if fp is None or not u.fused_at_start:
            return None, ""
        kind, val = fp
        cands = [p for p in self.parts[temp] if p.fp_matches(kind, val) and u not in p.units]
        if not cands:
            return None, ""
        # post-trim values: confirm / tie-break (same temperature only)
        scored = []
        for p in cands:
            d = post_diff(u.post, p.post) if p.post else None
            scored.append((d, p))
        known = sorted([s for s in scored if s[0] is not None], key=lambda s: s[0])
        # a candidate that FAILED before its post-trim tests cannot be scored. If such a failing candidate exists,
        # a passing candidate is only accepted on a very close match (a retest normally holds earlier FAILS).
        unscored_fail = any(d is None and not p.passed for d, p in scored)
        limit = SAME_PART_MAX_DIFF / 3 if unscored_fail else SAME_PART_MAX_DIFF
        if known and known[0][0] <= limit and (len(known) == 1 or known[1][0] > 1.5 * known[0][0]):
            return known[0][1], "eFuse + post-trim values"
        if strict:
            return None, ""
        # drop candidates that the values prove are different parts (room only: at hot/cold the same part
        # re-measures too differently for the values to rule a part out)
        cands = [p for d, p in scored if d is None or d < DIFF_PART_MIN_DIFF or temp not in ("Room", "PostBake")]
        if not cands:
            return None, ""
        if len(cands) == 1:
            return cands[0], "eFuse" if kind == "word" else "eFuse read-back"
        failing = [p for p in cands if not p.passed]
        if avoid and len(failing) > 1:
            # several failing parts share this fuse data: prefer one not already tested in this stage
            fresh_pick = [p for p in failing if p.no not in avoid]
            failing = fresh_pick or failing
        if len(failing) == 1:
            return failing[0], "eFuse (only failing part with this fuse data)"
        pick = (failing or cands)[-1]
        return pick, f"eFuse - AMBIGUOUS ({len(cands)} parts share this fuse data)"

    # ---- stage assignment ----------------------------------------------------------------------------
    def classify(self, f: STDFFileSummary, temp: str, seen_fresh: bool):
        if f.file_path in self.forced:
            name = self.forced[f.file_path]
            return name, "set by --stage"
        named = RETEST_NAME_RX.search(f.file_name) or RETEST_NAME_RX.search(f.lot_id)
        if named and (temp == "Room" or not seen_fresh):
            return ("Retest", "file / lot name says retest") if seen_fresh else \
                   ("Fresh Run", "named retest, but first file at this temperature")
        if named:
            fps = [u for u in f.units if u.fingerprint is not None and u.fused_at_start]
            hits = sum(1 for u in fps if (lambda p: p is not None and not p.passed)(self.find(u, temp)[0]))
            pool = sum(1 for c in self.pool_parts[temp] if c.anonymous and not c.passed)
            hits += min(pool, len(fps) - hits)
            if fps and hits / len(fps) < 0.5:
                return "Fresh Run", f"named retest, but only {hits} of {len(fps)} parts failed earlier here (new parts)"
            return "Retest", f"file name says retest ({hits} of {len(fps)} parts failed earlier here)"
        if not seen_fresh:
            return "Fresh Run", "first file at this temperature"
        fps = [u for u in f.units if u.fingerprint is not None]
        if temp == "Room":
            known = [u for u in f.units if u.fused_at_start is not None]
            fused = [u for u in known if u.fused_at_start]
            if known and len(fused) / len(known) >= 0.5:
                return "Retest", f"{len(fused)} of {len(known)} parts were already fused"
            return "Fresh Run", f"parts not fused yet ({len(known) - len(fused)} of {len(known)})"
        hits = 0
        for u in fps:
            p, _ = self.find(u, temp)
            if p is not None and not p.passed:
                hits += 1
        if fps and hits / len(fps) >= 0.5:
            return "Retest", f"{hits} of {len(fps)} parts failed earlier at {temp.lower()}"
        return "Fresh Run", f"only {hits} of {len(fps)} parts seen failing earlier"

    # ---- main ----------------------------------------------------------------------------------------
    def run(self):
        for f in self.files:
            f.units = build_units(f, self.exc)
            f.temperature = detect_temperature(f)
        self.order = self.insertion_order()
        for temp in self.order:
            tfiles = sorted([f for f in self.files if f.temperature == temp], key=lambda x: (x.start_t, x.file_name))
            if not tfiles:
                continue
            seen_fresh = False
            last_retest_date = None
            last_retest_no = 0
            round_no = 0
            stage_by_name: "OrderedDict[str, StageGroup]" = OrderedDict()
            used_by_stage: Dict[str, set] = {}
            rounds_of: Dict[int, int] = {}        # part no -> highest retest round it has been in
            for f in tfiles:
                kind, reason = self.classify(f, temp, seen_fresh)
                if f.file_path in self.forced:
                    name = kind
                    fresh = bool(re.search(r"fresh|first|initial|main", name, re.I))   # the given name decides
                elif kind == "Fresh Run":
                    name, fresh = "Fresh Run", True
                else:
                    # a new retest round starts when: the test date changes, the retest number in the file name
                    # changes (RETEST -> RETEST1 -> RETEST2), or parts of this file were already retested in
                    # the current round
                    d = f.start_str[:10]
                    m = RETEST_NO_RX.search(f.file_name)
                    num = int(m.group(1)) if m else 0
                    cur = stage_by_name.get(f"Retest {round_no}")
                    again = 0
                    ident = 0
                    if cur is not None:
                        in_round = {u.part.no for u in cur.units if u.part is not None and not u.part.anonymous}
                        for u in f.units:
                            p, _ = self.find(u, temp, strict=True)   # confirmed by post-trim values only
                            ident += u.fingerprint is not None and bool(u.fused_at_start)
                            if p is not None and p.no in in_round:
                                again += 1
                    again = again if again >= max(2, 0.2 * ident) else 0
                    # parts recognised by their fuse ID decide the round: a retest of parts that were never
                    # retested before is their 1st retest, even on a later date (e.g. a second initial batch)
                    known = [self.find(u, temp)[0] for u in f.units if u.fingerprint is not None]
                    known = [p for p in known if p is not None and not p.passed]
                    if not known and temp != TEMPS[0]:
                        # no usable fuse ID (the fails stopped before the fuse read): after room the Part IDs are
                        # unit serials, so an earlier FAIL with the same Part ID is the same chip
                        keys = {pid_key(u.pid) for u in f.units}
                        known = [p for p in self.parts[temp] if not p.passed and pid_key(p.units[-1].pid) in keys]
                    by_part = 1 + max((rounds_of.get(p.no, 0) for p in known), default=0) if known else None
                    if by_part is not None and by_part <= round_no:
                        round_no_file = by_part
                        last_retest_date, last_retest_no = d, num
                        name, fresh = f"Retest {round_no_file}", False
                        if round_no_file < round_no:
                            reason += f"; 1st retest of these parts" if round_no_file == 1 else \
                                      f"; retest no. {round_no_file} of these parts"
                        seen_fresh = seen_fresh or fresh
                        f.stage, f.stage_reason = name, reason
                        st = stage_by_name[name]
                        st.files.append(f)
                        st.file_paths.append(f.file_path)
                        self.process_file(f, temp, fresh, used_by_stage.setdefault(name, set()))
                        for u in f.units:
                            if getattr(u, "part", None) is not None:
                                rounds_of[u.part.no] = max(rounds_of.get(u.part.no, 0), round_no_file)
                        continue
                    if round_no == 0 or d != last_retest_date or again:
                        round_no += 1
                        if round_no > 1:
                            why = ("new test date" if d != last_retest_date else
                                   f"{again} part(s) already retested in Retest {round_no - 1}")
                            reason += f"; new round: {why}"
                    last_retest_date, last_retest_no = d, num
                    name, fresh = f"Retest {round_no}", False
                seen_fresh = seen_fresh or fresh
                f.stage, f.stage_reason = name, reason
                if name not in stage_by_name:
                    stage_by_name[name] = StageGroup(name, is_fresh=fresh, temperature=temp)
                st = stage_by_name[name]
                st.files.append(f)
                st.file_paths.append(f.file_path)
                self.process_file(f, temp, fresh, used_by_stage.setdefault(name, set()))
                if not fresh:
                    rn = int(name.split()[-1]) if name.split()[-1].isdigit() else 1
                    for u in f.units:
                        if getattr(u, "part", None) is not None:
                            rounds_of[u.part.no] = max(rounds_of.get(u.part.no, 0), rn)
            # cumulative numbers in STAGE order (Fresh Run, Retest 1, Retest 2 ...), whatever the test dates were
            order = list(stage_by_name.values())
            pos = {id(st): k for k, st in enumerate(order)}
            unit_stage = {}
            for st in order:
                for u in st.units:
                    unit_stage[id(u)] = pos[id(st)]
            for k, st in enumerate(order):
                pop = good = 0
                for p in self.parts[temp]:
                    seen = [u for u in p.units if unit_stage.get(id(u), 99) <= k]
                    if seen:
                        pop += 1
                        good += seen[-1].passed
                st.population, st.cum_good = pop, good
                st.collect_names()
                self.stages.append(st)
        # recovered per stage = parts that failed before and pass for the first time in this stage
        for st in self.stages:
            st.recovered = sum(1 for u in st.units if u.passed and u.part and u.part.units[0] is not u
                               and not any(x.passed for x in u.part.units[:u.part.units.index(u)]))
        StageGroup.show_temp = len({st.temperature for st in self.stages}) > 1
        self.link_temperatures()

    def insertion_order(self) -> List[str]:
        """Order of the test insertions, taken from the lot's own test dates: Room first (the parts are
        created there), then the other insertions by the date of their first file. Normal flow:
        Room -> Post-Bake -> Hot -> Cold."""
        first = {}
        for f in self.files:
            first[f.temperature] = min(first.get(f.temperature, f.start_t), f.start_t)
        rest = sorted([t for t in first if t != "Room"], key=lambda t: (first[t], TEMPS.index(t)))
        order = (["Room"] if "Room" in first else []) + rest
        if order != [t for t in TEMPS if t in first]:
            self.exc.add("Info", "Test order differs from normal flow", "", "",
                         " -> ".join(TEMP_LABEL[t] for t in order) + " (taken from the test dates)")
        return order

    def new_part(self, u: Unit, temp: str, how: str) -> Part:
        p = Part(temp, how)
        p.add(u)
        self.parts[temp].append(p)
        return p

    @staticmethod
    def failed_in_burn(p: "Part") -> bool:
        """True if the part's last result is a fail inside the eFuse BURN step (burn / read-margin / parity), i.e. the
        chip was burned but failed before its fuse word was logged. 'Fuse_Burn_Check' is the read-back BEFORE burning."""
        last = p.units[-1].last
        name = (last.file.sbr_defs.get(last.sb, {}).get("name") or "").upper()
        if not name or any(k in name for k in BURN_CHECK_KEYS):
            return False
        return any(k in name for k in BURN_FAIL_KEYS)

    def missing_in_rerun(self, u: Unit, temp: str, met: set, failed_only: bool = False) -> Optional["Part"]:
        """Re-run file, chip without a fuse read: an earlier FUSED chip that has not shown up in this file
        (earlier fails first, oldest first). None -> fall back to the unidentified pool / new part."""
        cands = [c for c in self.parts[temp] if c.no not in met and c.no not in self._born_here
                 and (c.word is not None or c.read) and not (failed_only and c.passed)]
        # a chip whose ID shows up in a LATER file at this temperature is that later insertion, not this one
        later = set()
        for g in self.files:
            if g.temperature == temp and g.start_t > u.file.start_t:
                for x in g.units:
                    if x.fingerprint is not None:
                        later.add(x.fingerprint[1])
        cands.sort(key=lambda c: ((c.word in later or c.read in later), c.passed, c.no))
        return cands[0] if cands else None

    def from_burned_pool(self, u: Unit, temp: str, used: set) -> Optional["Part"]:
        """An already-fused chip with no fuse record: it can only be an earlier fail that failed IN the burn step."""
        p = next((c for c in self.pool_parts[temp] if c.anonymous and not c.passed and c.no not in used
                  and self.failed_in_burn(c)), None)
        if p is not None:
            p.add(u)
            p.anonymous = False
            p.how = "earlier fail in the burn step (fuse code not logged then)"
            used.add(p.no)
        return p

    def from_pool(self, u: Unit, temp: str, used: set, identify: bool) -> Part:
        """The unit is one of the earlier UNIDENTIFIED fails (it was never fused). Take one of them.
        If there are none left, it must be a new part."""
        born = self._born_here
        free = [c for c in self.pool_parts[temp] if c.anonymous and not c.passed and c.no not in used
                and c.no not in born]
        same_id = [c for c in free if pid_key(c.units[-1].pid) == pid_key(u.pid)]
        p = (same_id or free or [None])[0]
        if p is None and not self.extra_as_new:
            # more insertions than earlier unidentified fails: the floor re-inserted the same failing parts
            # under a new Part ID (seen in several lots). Count them as the same parts, lot size unchanged.
            # (in an initial-test file a chip is not inserted twice under a new Part ID - that is a new chip)
            again = [c for c in self.pool_parts[temp] if c.anonymous and not c.passed and c.no not in born
                     and not (self._fresh_here and c.no in self._met_here)]
            if again:
                p = again[self._rr % len(again)]
                self._rr += 1
                p.add(u)
                if identify:
                    p.anonymous = False
                    p.how = "earlier unidentified fail, fused in a retest"
                self.exc.add("Info", "Re-insertion under new Part ID", u.file.file_name,
                             f"PID {u.pid}", "Treated as the same failing parts inserted again under a new Part ID "
                             "(lot size unchanged). Use --extra-as-new to count them as new parts instead.")
                return p
        if p is None:
            p = self.new_part(u, temp, "new part (more unidentified parts than earlier unidentified fails)")
            p.anonymous = not identify
            self.pool_parts[temp].append(p)
            born.add(p.no)
            self.exc.add("Warning", "Extra unidentified part (counted as new)", u.file.file_name,
                         f"PID {u.pid}", "Counted as a NEW part (lot size +1). Check the file belongs to this lot / stage.")
        else:
            p.add(u)
            if identify:
                p.anonymous = False
                p.how = "earlier unidentified fail, fused in a retest"
        used.add(p.no)
        return p

    def process_file(self, f: STDFFileSummary, temp: str, fresh: bool, used: set):
        """used = earlier unidentified fails already taken in this stage (shared by all files of the stage)"""
        rerun = False
        if fresh and self.parts[temp]:
            fused = [u for u in f.units if u.fused_at_start]
            seen = sum(1 for u in fused if self.find(u, temp)[0] is not None)
            rerun = len(f.units) > 0 and seen >= 0.25 * len(f.units)
            if rerun:
                self.exc.add("Info", "Initial-test file re-runs earlier chips", f.file_name, "",
                             f"{seen} of {len(f.units)} chips tested earlier")
        stage_parts = self._stage_parts.setdefault(id(used), set())
        self._born_here = set()
        met: set = set()                       # parts already met in THIS file
        self._met_here = met
        self._fresh_here = fresh
        units = list(f.units)
        noread = []
        if (fresh and rerun) or not fresh:
            # a chip that failed before the fuse read has no ID here. In a re-run or a retest it is one of the
            # earlier chips that did not show up (by ID) in this file, so it is handled after every chip with an ID.
            noread = [u for u in units if u.fingerprint is None and u.fused_at_start is None]
            units = [u for u in units if not (u.fingerprint is None and u.fused_at_start is None)]
        for u in units + noread:
            if u in noread:
                if not fresh and any(c.anonymous and not c.passed and c.no not in used and c.no not in met
                                     for c in self.pool_parts[temp]):
                    # retest: an earlier unidentified fail first (ID-less fails are retested as ID-less chips)
                    met.add(self.from_pool(u, temp, used, identify=False).no)
                    u.note = "earlier unidentified fail"
                    continue
                p = self.missing_in_rerun(u, temp, met, failed_only=not fresh)
                if p is not None:
                    p.add(u)
                    u.note = "no fuse read in this re-run - earlier chip not otherwise seen here"
                    stage_parts.add(p.no)
                    met.add(p.no)
                    continue
            strict = fresh and not (temp == TEMPS[0] and u.fused_at_start)
            p, how = self.find(u, temp, strict=strict, avoid=stage_parts)
            if p is not None and fresh and temp != TEMPS[0] and self.parts[TEMPS[0]] and u.fingerprint is not None:
                # at hot / cold the measurements cannot tell two chips apart. If the ROOM data has more chips with this
                # fuse code than this temperature has seen so far, the new insertion is another chip, not a repeat.
                kind, val = u.fingerprint
                n_room = sum(1 for q in self.parts[TEMPS[0]] if q.fp_matches(kind, val))
                n_here = sum(1 for q in self.parts[temp] if q.fp_matches(kind, val))
                if n_here < n_room:
                    p = None
            if p is None and fresh and temp != TEMPS[0] and self.parts[TEMPS[0]] and u.fingerprint is not None \
                    and u.fused_at_start:
                kind, val = u.fingerprint
                n_room = sum(1 for q in self.parts[TEMPS[0]] if q.fp_matches(kind, val))
                here = [q for q in self.parts[temp] if q.fp_matches(kind, val)]
                if n_room >= 1 and len(here) >= n_room:
                    # every room chip with this fuse code was already tested here -> this is one of them again
                    p, how = here[-1], f"eFuse (only {n_room} chip(s) in the lot have this code)"
            if p is not None:
                if fresh:
                    self.exc.add("Warning", "Repeat insertion in initial test", f.file_name,
                                 f"PID {u.pid} = Part {p.label}", f"Matched by {how}. Counted once (not as a new part).")
                elif p.passed:
                    self.exc.add("Warning", "Passed part retested", f.file_name,
                                 f"PID {u.pid} = Part {p.label}",
                                 f"Matched by {how}. Counted once; its latest result ({'PASS' if u.passed else 'FAIL'}) is used.")
                if "AMBIGUOUS" in how:
                    self.exc.add("Info", "Shared fuse code - match not unique", f.file_name, f"PID {u.pid}",
                                 how + ". Counts are not affected.")
                p.add(u)
                u.note = how
                stage_parts.add(p.no)
                met.add(p.no)
                continue
            if fresh and rerun and not u.fused_at_start and u.fused_at_start is not None or \
                    fresh and rerun and u.fingerprint is None:
                # re-run file: an unfused chip is one of the earlier unidentified fails (new only if none are left)
                free = [c for c in self.pool_parts[temp] if c.anonymous and not c.passed and c.no not in used]
                if free:
                    met.add(self.from_pool(u, temp, used, identify=u.fingerprint is not None).no)
                    continue
            if fresh:
                if temp == "Room" and u.fused_at_start:
                    if self.from_burned_pool(u, temp, used) is not None:
                        self.exc.add("Info", "Fused part matched to earlier burn-step fail", f.file_name, f"PID {u.pid}", "")
                        continue
                    self.exc.add("Warning", "Already-fused part in initial test (counted as new)", f.file_name, f"PID {u.pid}",
                                 "Burned in a run that is not in the attached files. Counted as a new part.")
                p = self.new_part(u, temp, "new part" if u.fingerprint else "no eFuse data (failed before the fuse burn)")
                self._born_here.add(p.no)
                met.add(p.no)
                if u.fingerprint is None and not u.passed:
                    p.anonymous = True
                    self.pool_parts[temp].append(p)
                continue
            # retest file, no earlier part found
            if u.fused_at_start:
                # fused, but no earlier fuse record: only an earlier fail IN the burn step can be this chip.
                # A chip that failed before the burn cannot arrive fused, so otherwise it is a chip from outside.
                if self.from_burned_pool(u, temp, used) is not None:
                    self.exc.add("Info", "Fused part matched to earlier burn-step fail", f.file_name, f"PID {u.pid}", "")
                elif temp != TEMPS[0] and any(c.anonymous and not c.passed and c.no not in used
                                              for c in self.pool_parts[temp]):
                    # at post-bake / hot / cold every chip is fused; an earlier fail here without a readable ID is
                    # this chip (same Part ID preferred)
                    self.from_pool(u, temp, used, identify=True)
                else:
                    self.new_part(u, temp, "not seen earlier at this temperature")
                    self.exc.add("Warning", "Retest part not found earlier (counted as new)", f.file_name, f"PID {u.pid}", "")
            else:
                # never fused before -> one of the earlier unidentified fails
                met.add(self.from_pool(u, temp, used, identify=u.fingerprint is not None).no)
                u.note = "earlier unidentified fail" + (", fused in this retest" if u.fingerprint else "")

    # ---- room <-> hot/cold ----------------------------------------------------------------------------
    def link_temperatures(self):
        temps = [t for t in self.order if self.parts[t]]
        if len(temps) < 2:
            return
        base = temps[0]                      # normally Room; if no room files were attached, the first insertion
        room = self.parts[base]
        for temp in temps[1:]:
            for p in self.parts[temp]:
                if p.word is None and p.read is None:
                    continue
                kind, val = ("word", p.word) if p.word is not None else ("read", p.read)
                cands = [r for r in room if r.fp_matches(kind, val)]
                good = [r for r in cands if r.passed and temp not in r.later]
                pick = (good or [r for r in cands if temp not in r.later] or [None])[0]
                if pick is not None:
                    p.room_part = pick
                    pick.later[temp] = p
                    if len(good) > 1:
                        self.exc.add("Info", f"Shared fuse code - {TEMP_LABEL[temp]} link not unique",
                                     p.units[0].file.file_name, f"PID {p.units[0].pid} = Part {p.label}",
                                     f"{len(good)} good {TEMP_LABEL[base]} parts share this fuse code; linked to "
                                     f"{pick.label}. Counts are not affected.")
                elif room:
                    self.exc.add("Info", f"{TEMP_LABEL[temp]} part not linked to {TEMP_LABEL[base]}",
                                 p.units[0].file.file_name, f"PID {p.units[0].pid}",
                                 f"Not linked to a {TEMP_LABEL[base]} part (file missing?).")

    # ---- final lot numbers ----------------------------------------------------------------------------
    def final(self):
        """Final lot numbers. A part is GOOD only if it passed every insertion the lot was screened at.
        - An insertion is a SCREEN when (almost) every good part went through it (>= 80 %), otherwise a SAMPLE.
        - Chips at a later insertion that carry no readable fuse ID cannot be linked to a room part. They are still
          chips of this lot: an unidentified FAIL is counted as lost from the good parts that have no result there,
          an unidentified PASS covers one of those parts.
        - A good part with no result at a screen is ON HOLD (neither good nor scrap)."""
        temps = [t for t in self.order if self.parts[t]]
        base = temps[0] if temps else "Room"
        lot = self.parts[base]
        lot_size = len(lot)
        base_good = [p for p in lot if p.passed]
        alive = list(base_good)
        hold: "OrderedDict[int, Tuple[Part, str]]" = OrderedDict()
        lost, untested, lost_unid = Counter(), Counter(), Counter()
        for t in temps[1:]:
            keep = []
            for p in alive:
                q = p.later.get(t)
                if q is not None and not q.passed:
                    lost[t] += 1
                else:
                    keep.append(p)
            alive = keep
            unl = [q for q in self.parts[t] if q.room_part is None]
            u_fail = [q for q in unl if not q.passed]
            u_pass = [q for q in unl if q.passed]
            # compare with the parts still good at this point (passed everything before this insertion)
            n_linked = sum(1 for p in alive if t in p.later)
            screen = bool(alive) and (n_linked + len(unl)) >= 0.8 * len([p for p in alive if p.no not in hold])
            missing = [p for p in alive if t not in p.later and p.no not in hold]
            pool = missing if (screen or missing) else [p for p in alive if p.no not in hold]
            k = min(len(u_fail), len(pool))
            for p in pool[:k]:
                alive.remove(p)
            lost[t] += k
            lost_unid[t] += k
            if k:
                self.exc.add("Info", f"Unreadable fuse ID - fails at {TEMP_LABEL[t]}", "", "",
                             f"{k} chip(s), counted as lost")
            if len(u_fail) > k:
                self.exc.add("Warning", f"Extra chips at {TEMP_LABEL[t]}", "", "",
                             f"{len(u_fail) - k} failing chip(s) not matched to this lot")
            if screen:
                missing = [p for p in missing if p in alive]
                missing = missing[min(len(u_pass), len(missing)):]
                for p in missing:
                    hold[p.no] = (p, t)
                    untested[t] += 1
            elif base_good:
                self.exc.add("Info", f"Sample insertion - {TEMP_LABEL[t]}", "", "",
                             f"{n_linked + len(unl)} of {len(alive)} good parts tested")
        gaps = self.stage_gaps(temps, hold)
        for g in gaps:
            self.exc.add(g["severity"], g["title"], g["file"], g["parts"], g["detail"])
        good = sum(1 for p in alive if p.no not in hold)
        first_pass = sum(1 for p in lot if p.first_passed)

        def rec(x):
            return x.passed and not x.first_passed
        # recovered = parts that passed only in a retest stage (same as the waterfall's Recovered column)
        recovered = sum(st.recovered for st in self.stages if not st.is_fresh)
        return {"base": base, "lot_size": lot_size, "good": good, "lost": lost, "first_pass": first_pass,
                "recovered": recovered, "temps": temps, "untested": untested, "hold": len(hold),
                "lost_unid": lost_unid, "gaps": gaps}

    @staticmethod
    def _where(q):
        u = q.units[-1]
        return f"{u.file.file_name} PID {u.pid} ({'PASS' if q.passed else 'FAIL'})"

    def stage_gaps(self, temps, hold):
        """Chips that did not follow the test flow:
        - 'missing'  : passed every insertion so far but has no result at a screened insertion (ON HOLD). If it shows up
                       at a LATER insertion, that is said too (it skipped one insertion).
        - 'after_fail': failed an insertion (final result there) but was still tested at a later insertion."""
        base = temps[0] if temps else "Room"
        gaps = []
        for p, t in hold.values():
            u = p.units[-1]
            later = [tt for tt in temps[temps.index(t) + 1:] if tt in p.later]
            if later:
                detail = (f"Passed every insertion before {TEMP_LABEL[t]}, but no {TEMP_LABEL[t]} test was found - "
                          f"it was tested at " + ", ".join(f"{TEMP_LABEL[tt]}: {self._where(p.later[tt])}" for tt in later)
                          + f". It skipped {TEMP_LABEL[t]}, or its {TEMP_LABEL[t]} test could not be matched to it.")
                title = f"Skipped {TEMP_LABEL[t]} - tested later"
            else:
                detail = (f"Passed every insertion before {TEMP_LABEL[t]}, but no {TEMP_LABEL[t]} test was found. "
                          f"Counted ON HOLD (neither good nor scrap) - check whether it went to {TEMP_LABEL[t]}.")
                title = f"On hold - not tested at {TEMP_LABEL[t]}"
            gaps.append(dict(kind="missing", severity="Warning", title=title, stage=t, part=p.label,
                             file=u.file.file_name, parts=f"PID {u.pid} = Part {p.label}", detail=detail,
                             later=[TEMP_LABEL[tt] for tt in later]))
        for p in self.parts[base]:
            chain = [(base, p)] + [(tt, p.later[tt]) for tt in temps[1:] if tt in p.later]
            for i, (t, q) in enumerate(chain[:-1]):
                if q.passed:
                    continue
                nxt = chain[i + 1]
                gaps.append(dict(kind="after_fail", severity="Info", title=f"Failed {TEMP_LABEL[t]}, still tested at {TEMP_LABEL[nxt[0]]}",
                                 stage=nxt[0], part=p.label, file=nxt[1].units[-1].file.file_name,
                                 parts=f"PID {nxt[1].units[-1].pid} = Part {nxt[1].label} (from {p.label})",
                                 detail=f"Final {TEMP_LABEL[t]} result was FAIL ({self._where(q)}), yet the chip was tested "
                                        f"at {TEMP_LABEL[nxt[0]]}: {self._where(nxt[1])}. It stays a fail in the lot result.",
                                 later=[TEMP_LABEL[nxt[0]]]))
                break
        return gaps


def temperature_summaries(stages: List["StageGroup"]):
    """Per temperature: parts tested there, final pass / fail there, yield, test runs (after its last stage)."""
    out = OrderedDict()
    for st in stages:
        d = out.setdefault(st.temperature, {"runs": 0})
        d["runs"] += st.total_runs
        d["parts"], d["good"] = st.population, st.cum_good          # last stage of the temperature wins
        d["last"] = st
    for d in out.values():
        d["fail"] = d["parts"] - d["good"]
        d["yield"] = d["good"] / d["parts"] if d["parts"] else 0.0
    return out


# ----------------------------------------------------------------------------------------------------------
# Console output
# ----------------------------------------------------------------------------------------------------------
def print_stage_table(stage: StageGroup):
    print("=" * 80)
    print(f" STAGE: {stage.display_name.upper()} ")
    print("=" * 80)
    print(f"Files Processed: {len(stage.files)}")
    if stage.lot_ids:
        print(f"Lot ID(s)      : {', '.join(sorted(stage.lot_ids))}")
    print("-" * 80)
    print(f"TOTAL TESTED   : {stage.total_parts:,} parts ({stage.total_runs:,} test runs)")
    print(f"TOTAL PASSED   : {stage.pass_parts:,} (Bin 1)")
    print(f"TOTAL FAILED   : {stage.fail_parts:,}")
    print(f"STAGE YIELD    : {stage.yield_pct:.2f} %")
    print("-" * 80)
    print(f"{'File Name':<70} | {'Parts':>6} | {'Pass':>6} | {'Runs':>6}")
    for s in stage.files:
        print(f"{s.file_name[-70:]:<70} | {len(s.units):>6d} | {sum(u.passed for u in s.units):>6d} | {len(s.runs):>6d}")
    print()


def print_overall_waterfall(tracker: LotTracker, fin):
    line_w = 100
    print("=" * line_w)
    print(" " * 30 + "CUMULATIVE LOT RECOVERY & YIELD WATERFALL")
    print("=" * line_w)
    print(f"{'Stage':<30} | {'Tested':>7} | {'Pass':>6} | {'Fail':>6} | {'Cumul Pass':>10} | {'Cumul Yield':>11} | {'Runs':>6}")
    print("-" * line_w)
    summ = temperature_summaries(tracker.stages)
    multi = len(summ) > 1
    for st in tracker.stages:
        cy = st.cum_good / st.population * 100 if st.population else 0
        print(f"{st.display_name:<30} | {st.total_parts:>7d} | {st.pass_parts:>6d} | {st.fail_parts:>6d} | "
              f"{st.cum_good:>10d} | {cy:>10.2f}% | {st.total_runs:>6d}")
        if multi and summ[st.temperature]["last"] is st:
            d = summ[st.temperature]
            lab = f"{TEMP_LABEL[st.temperature]} SUMMARY"
            print(f"{lab:<30} | {d['parts']:>7d} | {d['good']:>6d} | {d['fail']:>6d} | "
                  f"{d['good']:>10d} | {d['yield'] * 100:>10.2f}% | {d['runs']:>6d}")
            print("-" * line_w)
    print("=" * line_w)
    ls = fin["lot_size"]
    y = fin["good"] / ls * 100 if ls else 0
    print(f"LOT SIZE (UNIQUE PARTS)        : {ls:,}")
    print(f"FINAL TOTAL BIN 1 PASS COUNT   : {fin['good']:,}")
    print(f"FINAL NET LOT SCRAP / FAILS    : {ls - fin['good'] - fin['hold']:,}")
    for t, n in fin["lost"].items():
        print(f"  of which lost at {t:<12}: {n:,}")
    for t, n in fin["untested"].items():
        print(f"ON HOLD - NOT TESTED AT {t.upper():<7}: {n:,}")
    print(f"FINAL OVERALL LOT YIELD        : {y:.2f} %")
    print(f"Exceptions to check            : {len(grouped_exceptions(tracker.exc.rows))} lines, {len(tracker.exc.rows)} cases (see 'Exceptions' sheet)")
    print("=" * line_w)


# ----------------------------------------------------------------------------------------------------------
# Excel output (same layout and colours as the original report, extra sheets at the end)
# ----------------------------------------------------------------------------------------------------------
def grouped_exceptions(rows):
    """One line per (severity, type, file): parts listed together, so a file with many similar cases is one row."""
    order = {"Error": 0, "Warning": 1, "Info": 2}
    groups: "OrderedDict[tuple, list]" = OrderedDict()
    for sev, kind, fname, parts, details in rows:
        groups.setdefault((sev, kind, fname), []).append((parts, details))
    out = []
    for (sev, kind, fname), items in groups.items():
        parts = "; ".join(p for p, _ in items if p)
        details = items[0][1] if len({d for _, d in items}) == 1 else items[0][1] + f" (and {len(items) - 1} similar)"
        out.append((sev, kind, fname, len(items), parts, details))
    return sorted(out, key=lambda x: order.get(x[0], 3))


def export_multi_stage_excel(output_excel_path: str, tracker: LotTracker, fin, all_files, skipped, scope=None,
                             extra_sheets=None):
    """extra_sheets: optional function(workbook) called before saving (e.g. the per-file datalog sheets)."""
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        print("[Warning] openpyxl not found. Skipping Excel export.  (pip install openpyxl)")
        return

    stages = tracker.stages
    wb = openpyxl.Workbook()
    header_fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    title_font = Font(name="Calibri", size=14, bold=True, color="1F497D")
    sub_title_font = Font(name="Calibri", size=12, bold=True, color="1F497D")
    bold_font = Font(name="Calibri", size=11, bold=True)
    regular_font = Font(name="Calibri", size=11)
    note_font = Font(name="Calibri", size=10, italic=True, color="595959")
    pass_fill = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
    fail_fill = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")
    accent_fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
    highlight_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    thin_border = Border(
        left=Side(style='thin', color='D9D9D9'),
        right=Side(style='thin', color='D9D9D9'),
        top=Side(style='thin', color='D9D9D9'),
        bottom=Side(style='thin', color='D9D9D9')
    )
    PCT = "0.00%"
    GAIN = "+0.00%;-0.00%;0.00%"
    right = Alignment(horizontal="right")

    def header_row(ws, row, headers, left_cols=(1,)):
        for c_idx, h in enumerate(headers, start=1):
            c = ws.cell(row=row, column=c_idx, value=h)
            c.font = header_font
            c.fill = header_fill
            c.alignment = Alignment(horizontal="left" if c_idx in left_cols else "center", wrap_text=True)

    # 1. Executive Summary Sheet ------------------------------------------------------------------------
    scoped = scope is not None
    shown = [st for st in stages if not scoped or st.temperature == scope]
    ws_ov = wb.active
    ws_ov.title = "Lot Final Summary"
    ws_ov.views.sheetView[0].showGridLines = False
    ws_ov["A1"] = "OVERALL LOT MULTI-STAGE RETEST SUMMARY" + (f" - {TEMP_LABEL[scope].upper()} ONLY" if scoped else "")
    ws_ov["A1"].font = title_font
    ws_ov["A2"] = (f"Generated {datetime.datetime.now().strftime('%d %b %Y %H:%M')}   |   Analyzer v{VERSION}   |   "
                   f"Scope: {'All insertions' if not scoped else TEMP_LABEL[scope]}")
    ws_ov["A2"].font = note_font

    lot_size = fin["lot_size"]
    used_files = sorted([f for st in shown for f in st.files], key=lambda f: (f.start_t, f.file_name))
    ws_ov.sheet_properties.outlinePr.summaryBelow = False      # +/- button sits on the visible row above the list
    label_fill = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")

    def info_cells(row, label, val):
        c1 = ws_ov.cell(row=row, column=1, value=label)
        c1.font, c1.fill, c1.border = bold_font, label_fill, thin_border
        c2 = ws_ov.cell(row=row, column=2, value=val)
        c2.font, c2.border = regular_font, thin_border
        c2.alignment = Alignment(horizontal="left")
        ws_ov.merge_cells(start_row=row, start_column=2, end_row=row, end_column=11)
        return c2

    def collapsible(row, label, first_val, more):
        """Visible row = label (+N) and the first value; the rest sit in hidden grouped rows - the + button
        in the left margin opens them (no macros needed)."""
        info_cells(row, f"{label}  (+{len(more)} more)" if more else label, first_val)
        r = row + 1
        for v in more:
            ws_ov.cell(row=r, column=2, value=v).font = regular_font
            ws_ov.merge_cells(start_row=r, start_column=2, end_row=r, end_column=11)
            r += 1
        if more:
            ws_ov.row_dimensions.group(row + 1, r - 1, hidden=True, outline_level=1)
        return r

    def uniq(seq):
        out = []
        for x in seq:
            if x and x not in out:
                out.append(x)
        return out

    def day(t):
        return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%d %b %Y") if t else ""

    def span(files_):
        ts = [f.start_t for f in files_ if f.start_t]
        if not ts:
            return ""
        a_, b_ = day(min(ts)), day(max(ts))
        return a_ if a_ == b_ else f"{a_}  to  {b_}"

    r0 = 4
    lots = uniq(f.lot_id for f in used_files)
    r0 = collapsible(r0, "Lot ID(s)", lots[0] if lots else "", lots[1:])
    names = [f.file_name for f in used_files]
    r0 = collapsible(r0, "STDF File(s)", names[0] if names else "", names[1:])
    types = uniq(f.part_type for f in used_files)
    r0 = collapsible(r0, "Part Type(s)", types[0] if types else "", types[1:])
    flow = [t for t in tracker.order if any(st.temperature == t for st in stages)]
    info_cells(r0, "Initial Lot Size (Fresh Tested)", lot_size).number_format = "#,##0"
    info_cells(r0 + 1, "Total Stages Tracked", len(shown))
    info_cells(r0 + 2, "Test Flow (by test date)", "  ->  ".join(TEMP_LABEL[t] for t in flow))
    r0 += 3

    table_start = r0 + 1
    ws_ov.cell(row=table_start, column=1, value="Multi-Stage Recovery Waterfall").font = sub_title_font

    headers = [
        "Stage Name", "Tested Count", "Pass (Bin 1)", "Fail Count",
        "Stage Yield (%)", "Cumulative Bin 1", "Cumulative Lot Yield (%)", "Yield Gain (%)",
        "Test Runs", "Recovered (Bin 1)", "Test Date(s)",
    ]
    header_row(ws_ov, table_start + 1, headers, left_cols=(1, 11))
    ws_ov.row_dimensions[table_start + 1].height = 30
    band_font = Font(name="Calibri", size=11, bold=True, color="1F497D")
    band_border = Border(bottom=Side(style="medium", color="1F497D"))
    INT = "#,##0"

    curr_r = table_start + 2
    summ = temperature_summaries(stages)
    multi = len(summ) > 1
    prev_temp, prev_yield = None, 0.0
    for stg in shown:
        if stg.temperature != prev_temp:
            prev_yield = None
            prev_temp = stg.temperature
            if multi or scoped:
                tf = [f for st in stages if st.temperature == stg.temperature for f in st.files]
                band = f"{TEMP_LABEL[stg.temperature].upper()}      {span(tf)}      {len(tf)} file(s)"
                for ci in range(1, len(headers) + 1):
                    c = ws_ov.cell(row=curr_r, column=ci, value=band if ci == 1 else None)
                    c.font, c.border = band_font, band_border
                ws_ov.row_dimensions[curr_r].height = 20
                curr_r += 1
        cumul_yield = stg.cum_good / stg.population if stg.population else 0.0
        vals = [stg.display_name, stg.total_parts, stg.pass_parts, stg.fail_parts,
                stg.pass_parts / stg.total_parts if stg.total_parts else 0.0,
                stg.cum_good, cumul_yield,
                (cumul_yield - prev_yield) if prev_yield is not None else "-",
                stg.total_runs, stg.recovered if not stg.is_fresh else "-", span(stg.files)]
        for ci, v in enumerate(vals, start=1):
            c = ws_ov.cell(row=curr_r, column=ci, value=v)
            c.border = thin_border
            c.font = regular_font
            if 1 < ci < 11:
                c.alignment = right
                if isinstance(v, int):
                    c.number_format = INT
            elif ci == 11:
                c.alignment = Alignment(horizontal="left", indent=1)
        ws_ov.cell(row=curr_r, column=5).number_format = PCT
        ws_ov.cell(row=curr_r, column=7).number_format = PCT
        ws_ov.cell(row=curr_r, column=3).fill = pass_fill
        ws_ov.cell(row=curr_r, column=4).fill = fail_fill if stg.fail_parts else PatternFill()
        if prev_yield is not None:
            ws_ov.cell(row=curr_r, column=8).number_format = GAIN
        prev_yield = cumul_yield
        curr_r += 1
        # short summary after each temperature (only when the lot was tested at more than one temperature)
        if multi and summ[stg.temperature]["last"] is stg:
            d = summ[stg.temperature]
            vals = [f"{TEMP_LABEL[stg.temperature].upper()} SUMMARY", d["parts"], d["good"], d["fail"],
                    d["yield"], d["good"], d["yield"], "", d["runs"],
                    sum(st.recovered for st in stages if st.temperature == stg.temperature and not st.is_fresh), ""]
            for ci, v in enumerate(vals, start=1):
                c = ws_ov.cell(row=curr_r, column=ci, value=v)
                c.border = thin_border
                c.font = bold_font
                c.fill = accent_fill
                if ci > 1:
                    c.alignment = right
                    if isinstance(v, int):
                        c.number_format = INT
            ws_ov.cell(row=curr_r, column=5).number_format = PCT
            ws_ov.cell(row=curr_r, column=7).number_format = PCT
            curr_r += 2

    # Final summary box
    curr_r += 1
    good = fin["good"]
    final_yield = good / lot_size if lot_size else 0.0
    if not scoped:
        ws_ov.cell(row=curr_r, column=1, value="Final Lot Result").font = sub_title_font
        curr_r += 1
        final_kpis = [
            ("FINAL TOTAL BIN 1 PASS COUNT", good, INT),
            ("FINAL NET LOT FAILS (SCRAP)", lot_size - good - fin["hold"], INT),
            ("FINAL OVERALL LOT YIELD", final_yield, PCT),
            ("TOTAL RECOVERY FROM RETEST(S)", fin["recovered"] / lot_size if lot_size else 0.0, GAIN),
        ]
        for t in fin["temps"][1:]:
            final_kpis.append((f"PARTS LOST AT {TEMP_LABEL[t].upper()}", fin["lost"].get(t, 0), INT))
            if fin["untested"].get(t):
                final_kpis.append((f"ON HOLD - NOT TESTED AT {TEMP_LABEL[t].upper()}", fin["untested"][t], INT))
    else:
        d = summ.get(scope)
        lab = TEMP_LABEL[scope].upper()
        ws_ov.cell(row=curr_r, column=1, value=f"{TEMP_LABEL[scope]} Result").font = sub_title_font
        curr_r += 1
        final_kpis = [
            (f"PARTS TESTED AT {lab}", d["parts"] if d else 0, INT),
            (f"PASS (BIN 1) AT {lab}", d["good"] if d else 0, INT),
            (f"FAIL AT {lab}", d["fail"] if d else 0, INT),
            (f"YIELD AT {lab}", d["yield"] if d else 0.0, PCT),
            (f"RECOVERED BY RETEST AT {lab}",
             sum(st.recovered for st in shown if not st.is_fresh), INT),
        ]
        if scope != fin["base"]:
            final_kpis += [
                (f"LINKED TO {TEMP_LABEL[fin['base']].upper()} PARTS",
                 sum(1 for q in tracker.parts[scope] if q.room_part is not None), INT),
                (f"GOOD PARTS LOST AT {lab}", fin["lost"].get(scope, 0), INT),
            ]
            if fin["untested"].get(scope):
                final_kpis.append((f"ON HOLD - NOT TESTED AT {lab}", fin["untested"][scope], INT))
        final_kpis.append(("WHOLE LOT: FINAL GOOD / LOT SIZE", f"{good:,} / {lot_size:,}  ({final_yield:.2%})", None))
    for label, val, fmt in final_kpis:
        c1 = ws_ov.cell(row=curr_r, column=1, value=label)
        c2 = ws_ov.cell(row=curr_r, column=2, value=val)
        c1.font = Font(name="Calibri", size=11, bold=True, color="006100")
        c2.font = Font(name="Calibri", size=12, bold=True, color="006100")
        c1.fill = highlight_fill
        c2.fill = highlight_fill
        c1.border = thin_border
        c2.border = thin_border
        if fmt:
            c2.number_format = fmt
        c2.alignment = Alignment(horizontal="left")
        ws_ov.merge_cells(start_row=curr_r, start_column=2, end_row=curr_r, end_column=3)
        ws_ov.row_dimensions[curr_r].height = 18
        curr_r += 1
    ws_ov.freeze_panes = None
    ws_ov.page_setup.orientation = "landscape"
    ws_ov.page_setup.fitToWidth = 1
    ws_ov.sheet_properties.pageSetUpPr.fitToPage = True

    # Individual Stage sheets (same layout as before) -----------------------------------------------------
    used_titles = set()
    tab_colour = {"Room": "1F497D", "PostBake": "7030A0", "Hot": "C55A11", "Cold": "2E75B6"}
    for stg in shown:
        title = re.sub(r'[\/\\\?\*\[\]:]', '_', stg.name if len(fin["temps"]) == 1
                       else f"{stg.temperature} - {stg.name}")[:28]
        while title in used_titles:
            title = title[:26] + "_" + str(len(used_titles))
        used_titles.add(title)
        ws = wb.create_sheet(title=title)
        ws.sheet_properties.tabColor = tab_colour.get(stg.temperature, "1F497D")
        ws.views.sheetView[0].showGridLines = False
        ws.freeze_panes = "A5"
        ws["A1"] = f"{stg.display_name.upper()} - DETAIL BREAKDOWN"
        ws["A1"].font = title_font

        ws.cell(row=3, column=1, value="Per-File Breakdown").font = sub_title_font
        file_hdrs = ["File Name", "Total Tested", "Passed (Bin 1)", "Failed", "Yield (%)", "Test Runs", "How Stage Was Decided"]
        header_row(ws, 4, file_hdrs)
        r = 5
        for f in stg.files:
            n = len(f.units)
            p = sum(u.passed for u in f.units)
            vals = [f.file_name, n, p, n - p, p / n if n else 0.0, len(f.runs), f.stage_reason]
            for ci, v in enumerate(vals, start=1):
                c = ws.cell(row=r, column=ci, value=v)
                c.border = thin_border
                if 1 < ci < 7:
                    c.alignment = right
            ws.cell(row=r, column=5).number_format = PCT
            r += 1
        # Stage Total
        tot = [f"{stg.name.upper()} TOTAL (unique parts)", stg.total_parts, stg.pass_parts, stg.fail_parts,
               stg.pass_parts / stg.total_parts if stg.total_parts else 0.0, stg.total_runs]
        for ci, v in enumerate(tot, start=1):
            c = ws.cell(row=r, column=ci, value=v)
            c.font = bold_font
            c.border = thin_border
            if ci > 1:
                c.alignment = right
        ws.cell(row=r, column=5).number_format = PCT

        for label, counts, names in (("Hard Bin Summary", stg.combined_hbins, stg.hbin_names),
                                     ("Soft Bin Summary", stg.combined_sbins, stg.sbin_names)):
            r += 3 if label.startswith("Hard") else 2
            ws.cell(row=r, column=1, value=label).font = sub_title_font
            r += 1
            col = "HBin" if label.startswith("Hard") else "SBin"
            header_row(ws, r, [col, "Status", "Bin Name", "Count", "Yield/Fallout (%)"], left_cols=(3,))
            r += 1
            for b in sorted(counts):
                cnt = counts[b]
                st = "PASS" if b == 1 else "FAIL"
                vals = [b, st, names.get(b, "") or ("PASS" if b == 1 else ""), cnt,
                        cnt / stg.total_parts if stg.total_parts else 0.0]
                for ci, v in enumerate(vals, start=1):
                    c = ws.cell(row=r, column=ci, value=v)
                    c.border = thin_border
                ws.cell(row=r, column=1).alignment = Alignment(horizontal="center")
                ws.cell(row=r, column=2).alignment = Alignment(horizontal="center")
                ws.cell(row=r, column=2).fill = pass_fill if st == "PASS" else fail_fill
                ws.cell(row=r, column=4).alignment = right
                ws.cell(row=r, column=5).alignment = right
                ws.cell(row=r, column=5).number_format = PCT
                r += 1

    # Files sheet -------------------------------------------------------------------------------------
    ws = wb.create_sheet(title="Files")
    ws["A1"] = "ATTACHED STDF FILES"
    ws["A1"].font = title_font
    hdr = ["#", "File Name", "Lot ID (header)", "Program", "Temperature", "Start Time (tester)", "Test Runs",
           "Unique Parts", "Retests Inside File", "Parts Already Fused at Start", "Stage", "How Stage Was Decided", "Used?"]
    header_row(ws, 3, hdr, left_cols=(2, 3, 4, 12, 13))
    r = 4
    numbered = list(enumerate(sorted(all_files, key=lambda x: x.start_t), start=1))
    for i, f in numbered:
        if scoped and getattr(f, "temperature", "") != scope:
            continue
        used = f.status == "Used"
        fused = sum(1 for u in f.units if u.fused_at_start)
        vals = [i, f.file_name, f.lot_id, f.job_name, TEMP_LABEL.get(f.temperature, "") if used else "",
                f.start_str, len(f.runs), len(f.units) if used else "", (len(f.runs) - len(f.units)) if used else "",
                fused if used else "", f.stage if used else "", f.stage_reason if used else "", f.status]
        for ci, v in enumerate(vals, start=1):
            c = ws.cell(row=r, column=ci, value=v)
            c.border = thin_border
        ws.cell(row=r, column=13).fill = pass_fill if used else fail_fill
        r += 1
    ws.auto_filter.ref = f"A3:M{max(r - 1, 3)}"
    ws.freeze_panes = "A4"

    # Parts sheet: ONE sheet with a selector (ALL / Room / Hot / Cold) that switches the list (formulas, no macros) --
    from openpyxl.worksheet.datavalidation import DataValidation
    from openpyxl.formatting.rule import FormulaRule
    from openpyxl.styles.differential import DifferentialStyle
    temps_used = [t for t in tracker.order if tracker.parts[t] and (not scoped or t == scope)]
    short = {t: TEMP_LABEL[t].split()[0] for t in TEMPS}                 # Room / Post-Bake / Hot / Cold
    file_no = {f.file_path: i for i, f in enumerate(sorted(all_files, key=lambda x: x.start_t), start=1)}
    sb_names = {}
    for st in stages:
        sb_names.update({b: n for b, n in st.sbin_names.items() if n})
    hdr = ["Part #", "Temperature", "Fuse Word (hex)", "Identified By", "Times Tested", "History (stage: file# / PID -> HBin)",
           "First Result", "Final Result", "Final HBin", "Final SBin", "Final Bin Name", "Linked Room Part"]
    rows = []
    for t in temps_used:
        for p in tracker.parts[t]:
            hist = "; ".join(f"{u.file.stage}: F{file_no[u.file.file_path]}/{u.pid}->" +
                             ",".join(str(x.hb) for x in u.runs) for u in p.units)
            last = p.units[-1].last
            rows.append([p.label, TEMP_LABEL[t],
                         f"{p.word:012X}" if p.word is not None else ("read %.6g" % p.read if p.read else ""),
                         "unidentified (no eFuse data)" if p.anonymous else p.how, sum(len(u.runs) for u in p.units), hist,
                         "PASS" if p.first_passed else "FAIL", "PASS" if p.passed else "FAIL", last.hb, last.sb,
                         sb_names.get(last.sb, ""), p.room_part.label if p.room_part else "", short[t]])
    n = max(len(rows), 1)

    ws = wb.create_sheet(title="Parts")
    wd = wb.create_sheet(title="PartsData")          # hidden source table for the Parts sheet
    SEL = "Parts!$B$3"
    for ci, h in enumerate(hdr + ["View", "Row in view"], start=1):
        wd.cell(row=1, column=ci, value=h)
    for i, row in enumerate(rows, start=2):
        for ci, v in enumerate(row, start=1):
            wd.cell(row=i, column=ci, value=v)
        # running number of the rows that belong to the selected view (blank if not selected)
        wd.cell(row=i, column=14, value=f'=IF(OR({SEL}="ALL",$M{i}={SEL}),MAX(N$1:N{i - 1})+1,"")')
    wd.sheet_state = "hidden"
    last_d = n + 1

    ws.sheet_view.showGridLines = False
    ws["A1"] = "PARTS - EVERY PHYSICAL PART AND WHERE IT WAS TESTED"
    ws["A1"].font = title_font
    ws.cell(row=3, column=1, value="Show:").font = Font(name="Calibri", size=12, bold=True, color="1F497D")
    sel = ws.cell(row=3, column=2, value="ALL")
    sel.font = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
    sel.fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
    sel.alignment = Alignment(horizontal="center", vertical="center")
    sel.border = Border(left=Side(style="medium", color="1F497D"), right=Side(style="medium", color="1F497D"),
                        top=Side(style="medium", color="1F497D"), bottom=Side(style="medium", color="1F497D"))
    ws.row_dimensions[3].height = 26
    options = ["ALL"] + [short[t] for t in temps_used]
    dv = DataValidation(type="list", formula1='"' + ",".join(options) + '"', allow_blank=False, showDropDown=False)
    dv.promptTitle = "Choose view"
    dv.prompt = "Pick " + " / ".join(options)
    dv.error = "Pick one of: " + ", ".join(options)
    ws.add_data_validation(dv)
    dv.add(sel)
    ws.cell(row=3, column=3, value="<- click the blue box and pick " + " / ".join(options)).font = note_font

    # counts for the selected view
    rngB, rngH, rngM = f"PartsData!$B$2:$B${last_d}", f"PartsData!$H$2:$H${last_d}", f"PartsData!$M$2:$M${last_d}"
    counts = [("Parts", f'=IF({SEL}="ALL",COUNTA({rngB}),COUNTIF({rngM},{SEL}))'),
              ("PASS", f'=IF({SEL}="ALL",COUNTIF({rngH},"PASS"),COUNTIFS({rngM},{SEL},{rngH},"PASS"))'),
              ("FAIL", f'=IF({SEL}="ALL",COUNTIF({rngH},"FAIL"),COUNTIFS({rngM},{SEL},{rngH},"FAIL"))')]
    for k, (lab, f) in enumerate(counts):
        c1 = ws.cell(row=5, column=1 + 2 * k, value=lab)
        c2 = ws.cell(row=5, column=2 + 2 * k, value=f)
        c1.font = sub_title_font
        c2.font = Font(name="Calibri", size=12, bold=True)
        c2.alignment = Alignment(horizontal="left")
        c2.fill = pass_fill if lab == "PASS" else fail_fill if lab == "FAIL" else accent_fill
    HR = 8
    header_row(ws, HR, hdr, left_cols=(2, 3, 4, 6, 11))
    col = "ABCDEFGHIJKL"
    for k in range(1, n + 1):
        r = HR + k
        for ci, cl in enumerate(col, start=1):
            ws.cell(row=r, column=ci,
                    value=f'=IFERROR(INDEX(PartsData!${cl}$2:${cl}${last_d},MATCH({k},PartsData!$N$2:$N${last_d},0)),"")')
    first, lastr = HR + 1, HR + n
    area = f"A{first}:L{lastr}"
    for rng_cf in (f"A{first}:G{lastr}", f"I{first}:L{lastr}"):      # H gets its own rules (fill + border)
        ws.conditional_formatting.add(rng_cf, FormulaRule(formula=[f'$A{first}<>""'], border=thin_border))
    cf_pass = PatternFill(bgColor="E2EFDA", fill_type="solid")     # conditional formats need bgColor
    cf_fail = PatternFill(bgColor="FCE4D6", fill_type="solid")
    ws.conditional_formatting.add(f"H{first}:H{lastr}", FormulaRule(formula=[f'$H{first}="PASS"'], fill=cf_pass, border=thin_border))
    ws.conditional_formatting.add(f"H{first}:H{lastr}", FormulaRule(formula=[f'$H{first}="FAIL"'], fill=cf_fail, border=thin_border))
    ws.freeze_panes = f"A{HR + 1}"
    widths = {"A": 10, "B": 14, "C": 17, "D": 30, "E": 13, "F": 70, "G": 12, "H": 12, "I": 11, "J": 11, "K": 30, "L": 17}
    for cl, w in widths.items():
        ws.column_dimensions[cl].width = w
    wb.calculation.fullCalcOnLoad = True

    # Exceptions --------------------------------------------------------------------------------------
    ws = wb.create_sheet(title="Exceptions")
    ws["A1"] = "EXCEPTIONS - THINGS TO CHECK"
    ws["A1"].font = title_font
    header_row(ws, 3, ["Severity", "Type", "File", "Count", "Part(s) / Info"], left_cols=(1, 2, 3, 5))
    r = 4
    order = {"Error": 0, "Warning": 1, "Info": 2}
    scope_files = {f.file_name for f in all_files if getattr(f, "temperature", "") == scope}

    def in_scope(row):
        if not scoped:
            return True
        _sev, kind, fname, _p, _d = row
        labels = [TEMP_LABEL[t] for t in TEMPS if TEMP_LABEL[t] in kind]
        if labels:
            return TEMP_LABEL[scope] in labels
        return fname in scope_files if fname else True
    for g in grouped_exceptions([x for x in tracker.exc.rows if in_scope(x)]):
        row = (g[0], g[1], g[2], g[3], g[4] or g[5])
        for ci, v in enumerate(row, start=1):
            c = ws.cell(row=r, column=ci, value=v)
            c.border = thin_border
            c.alignment = Alignment(vertical="top", wrap_text=ci == 5)
        ws.cell(row=r, column=1).fill = fail_fill if row[0] in ("Error", "Warning") else accent_fill
        r += 1
    if r == 4:
        ws.cell(row=4, column=1, value="None").font = regular_font

    # Auto-adjust widths (capped so long file names / notes stay readable)
    for sheet in wb.worksheets:
        if sheet.title in ("Parts", "PartsData"):
            continue
        if sheet.title == "Lot Final Summary":
            for cl, w in zip("ABCDEFGHIJK", (40, 13, 13, 13, 13, 14, 15, 13, 12, 14, 28)):
                sheet.column_dimensions[cl].width = w
            continue
        for col in sheet.columns:
            max_len = 0
            col_letter = get_column_letter(col[0].column)
            for cell in col:
                if cell.value is not None and cell.font not in (note_font, band_font) and len(str(cell.value)) <= 100 \
                        and not str(cell.value).startswith(("=", "PARTS - ", "Parts: ")):
                    max_len = max(max_len, len(str(cell.value)))
            cap = 60 if (sheet.title == "Lot Final Summary" and col_letter == "B") else 90
            sheet.column_dimensions[col_letter].width = min(max(max_len + 4, 12), cap)

    os.makedirs(os.path.dirname(os.path.abspath(output_excel_path)), exist_ok=True)
    if extra_sheets is not None:
        extra_sheets(wb)
    wb.save(output_excel_path)
    print(f"\n[Excel Exported]: {output_excel_path}")


# ----------------------------------------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------------------------------------
def read_stdf(path: str, args, keep_values: bool = False) -> "STDFFileSummary":
    s = STDFFileSummary(path, args.fingerprint, args.fuse_bits)
    s.keep_values = keep_values
    s.parse()
    return s


def load_files(paths: List[str], args, exc: Exceptions, reader=None):
    files, skipped, seen = [], [], {}
    excl = re.compile(args.exclude, re.I) if args.exclude else None
    for p in paths:
        try:
            s = reader(p) if reader else read_stdf(p, args)
        except Exception as e:  # noqa
            s = STDFFileSummary(p, args.fingerprint, args.fuse_bits)
            s.status = f"Left out: could not read ({e})"
            s.temperature = ""
            skipped.append(s)
            continue
        if SANITY_RX.search(" ".join((s.file_name, s.lot_id or "", s.job_name or ""))):
            s.status = "Left out: sanity check file"
        elif excl and excl.search(s.file_name):
            s.status = "Left out: name matches exclude list (" + args.exclude + ")"
        elif not s.runs:
            s.status = "Left out: no parts in file"
        elif s.md5 in seen:
            s.status = "Left out: identical copy of " + seen[s.md5].file_name
        if s.status != "Used":
            s.temperature = detect_temperature(s) if s.runs or s.job_name else ""
            skipped.append(s)
            continue
        seen[s.md5] = s
        files.append(s)
        if s.truncated:
            exc.add("Warning", "Incomplete STDF file", s.file_name, "",
                    s.truncated + " - the last part of the file was not read (file copied while still being written?).")
    return files, skipped


SCOPES = {"all": None, "room": "Room", "postbake": "PostBake", "hot": "Hot", "cold": "Cold"}


class Options:
    """Settings for one analysis (same names as the command-line options)."""
    def __init__(self, **kw):
        self.exclude = kw.get("exclude", DEFAULT_EXCLUDE)
        self.fingerprint = kw.get("fingerprint", DEFAULT_FINGERPRINT)
        self.fuse_bits = kw.get("fuse_bits", DEFAULT_FUSE_BITS)
        self.extra_as_new = kw.get("extra_as_new", False)
        self.allow_mixed_devices = kw.get("allow_mixed_devices", False)


class AnalysisError(Exception):
    pass


def analyze(paths: List[str], args, forced: Optional[Dict[str, str]] = None, log=print, reader=None):
    """Read the files of ONE lot and follow every part. Always reads ALL attached files (so chips can be
    matched across insertions); a report scope only changes what is shown."""
    exc = Exceptions()
    forced = forced or {}
    paths = list(OrderedDict.fromkeys(paths))
    if not paths:
        raise AnalysisError("No valid STDF files found.")
    log(f"STDF Multi-Stage Lot Yield Analyzer v{VERSION}")
    log(f"Reading {len(paths)} STDF file(s) ...")
    files, skipped = load_files(paths, args, exc, reader=reader)
    for s_ in skipped:
        log(f"  - left out: {s_.file_name}  ({s_.status})")
    if not files:
        raise AnalysisError("No usable STDF files.")
    variants = Counter(device_variant(f) for f in files)
    real = [v for v in variants if v != "?"]
    if len(real) > 1:
        msg = ", ".join(f"{v} ({n} files)" for v, n in variants.items())
        if not args.allow_mixed_devices:
            raise AnalysisError(f"Files from different device versions were attached: {msg}. "
                                f"Run one report per device, or allow mixed devices.")
        exc.add("Warning", "Mixed device versions", "", "", msg)
    jobs = Counter(f.job_name for f in files)
    if len(jobs) > 1:
        exc.add("Info", "Multiple test programs", "", "",
                "; ".join(f"{j} ({n} files)" for j, n in jobs.items()))
    tracker = LotTracker(files, forced, exc)
    tracker.extra_as_new = args.extra_as_new
    tracker.run()
    fin = tracker.final()
    return tracker, fin, files, skipped


def summary_dict(tracker: "LotTracker", fin, files, skipped, scope=None) -> dict:
    """Plain numbers of an analysis (used by the desktop app)."""
    summ = temperature_summaries(tracker.stages)
    ls = fin["lot_size"]
    out = {
        "version": VERSION, "scope": scope or "all",
        "lot_ids": sorted({f.lot_id for f in files if f.lot_id}),
        "part_types": sorted({f.part_type for f in files if f.part_type}),
        "flow": [t for t in tracker.order if t in summ],
        "labels": TEMP_LABEL,
        "lot_size": ls, "good": fin["good"], "scrap": ls - fin["good"] - fin["hold"], "hold": fin["hold"],
        "yield": fin["good"] / ls if ls else 0.0,
        "lost": dict(fin["lost"]), "untested": dict(fin["untested"]),
        "areas": {}, "stages": [], "files": [],
        "exceptions": len(grouped_exceptions(tracker.exc.rows)),
        "gaps": [{k: g[k] for k in ("kind", "title", "stage", "part", "file", "parts", "detail", "later")}
                 for g in fin.get("gaps", []) if scope is None or g["stage"] == scope],
    }
    sb_names = {}
    for st in tracker.stages:
        sb_names.update({b: n for b, n in st.sbin_names.items() if n})
    for t, d in summ.items():
        tf = [f for st in tracker.stages if st.temperature == t for f in st.files]
        ts = [f.start_t for f in tf if f.start_t]
        bins = Counter()
        for q in tracker.parts[t]:
            if not q.passed:
                lr = q.units[-1].last
                bins[(lr.hb, lr.sb)] += 1
        out["areas"][t] = {"first": min(ts) if ts else 0, "last": max(ts) if ts else 0,
                           "lost": fin["lost"].get(t, 0), "hold": fin["untested"].get(t, 0),
                           "fail_bins": [{"hb": hb, "sb": sb, "name": sb_names.get(sb, ""), "count": n}
                                         for (hb, sb), n in bins.most_common(8)],
                           "parts": d["parts"], "good": d["good"], "fail": d["fail"], "yield": d["yield"],
                           "runs": d["runs"],
                           "recovered": sum(st.recovered for st in tracker.stages
                                            if st.temperature == t and not st.is_fresh),
                           "linked": sum(1 for q in tracker.parts[t] if q.room_part is not None),
                           "files": sum(len(st.files) for st in tracker.stages if st.temperature == t)}
    for st in tracker.stages:
        out["stages"].append({"temp": st.temperature, "name": st.name, "tested": st.total_parts,
                              "pass": st.pass_parts, "fail": st.fail_parts, "runs": st.total_runs,
                              "cum_good": st.cum_good, "population": st.population,
                              "recovered": None if st.is_fresh else st.recovered,
                              "files": len(st.files)})
    for f in sorted(files + skipped, key=lambda x: (x.start_t, x.file_name)):
        used = f.status == "Used"
        out["files"].append({"name": f.file_name, "path": f.file_path, "temp": getattr(f, "temperature", ""),
                             "stage": f.stage if used else "", "used": used, "status": f.status,
                             "start": f.start_str,
                             "parts": len(f.units) if used else 0,
                             "pass": sum(u.passed for u in f.units) if used else 0})
    return out


def main():
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--profile")
    known, _ = pre.parse_known_args()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import profiles                        # device profile first: it sets the defaults shown below
    prof = profiles.load(known.profile) if known.profile else profiles.saved()   # else the one chosen in the app
    profiles.apply(prof, sys.modules[__name__])
    print(f"Device profile: {profiles.current()['name']}")
    parser = argparse.ArgumentParser(
        description="STDF Multi-Stage Lot Yield Analyzer v2: counts physical parts across Fresh Run / Retest files "
                    "and Room / Post-Bake / Hot / Cold, with recovery waterfall.")
    parser.add_argument("-i", "--input", nargs="+",
                        help="STDF files and/or folders of ONE lot (folders are searched including subfolders). "
                             "Stages and temperatures are detected automatically.")
    parser.add_argument("--stage", nargs=2, action="append", metavar=("STAGE_NAME", "PATH"),
                        help="Force a stage by hand: --stage 'Fresh Run' path --stage 'Retest 1' path ...")
    parser.add_argument("-o", "--output-excel", default=None, help="Output path for the Excel report (.xlsx)")
    parser.add_argument("--scope", default="all", choices=list(SCOPES),
                        help="Area to report: all (default), room, postbake, hot or cold. All files are still read "
                             "so parts are matched across insertions; only the chosen area is shown.")
    parser.add_argument("--exclude", default=DEFAULT_EXCLUDE,
                        help=f"Regex of file names to leave out (default: {DEFAULT_EXCLUDE}); use '' to keep all. "
                             f"Sanity-check files are always left out.")
    parser.add_argument("--fingerprint", default=DEFAULT_FINGERPRINT,
                        help=f"Name (or part of the name) of the fuse read-back test (default: {DEFAULT_FINGERPRINT})")
    parser.add_argument("--fuse-bits", default=DEFAULT_FUSE_BITS,
                        help=f"Suite name of the post-burn per-bit eFuse tests (default: {DEFAULT_FUSE_BITS})")
    parser.add_argument("--profile", default=None,
                        help="Device profile JSON (fuse test names, matching tests, device pattern). Without it parts "
                             "are followed by Part ID only.")
    parser.add_argument("--no-file-sheets", action="store_true",
                        help="Leave out the per-file datalog sheets (one sheet per STDF file, every test value)")
    parser.add_argument("--extra-as-new", action="store_true",
                        help="Count retest insertions beyond the number of earlier unidentified fails as NEW parts "
                             "(default: treat them as the same failing parts inserted again)")
    parser.add_argument("--allow-mixed-devices", action="store_true",
                        help="Do not stop when files of different device versions (profile device_regex) are attached")
    args = parser.parse_args()

    forced: Dict[str, str] = {}
    paths: List[str] = []
    if args.stage:
        for stage_name, path in args.stage:
            fl = collect_stdf_files(path)
            if not fl:
                # an empty or missing stage folder is normal (e.g. no Retest 3 for this lot): skip it
                print(f"[Info] No STDF files for stage '{stage_name}' ({path}) - stage skipped.")
                continue
            for fp in fl:
                forced[fp] = stage_name
                paths.append(fp)
    if args.input:
        for item in args.input:
            fl = collect_stdf_files(item)
            if not fl:
                print(f"[Warning] No STDF files found in: {item}")
            paths.extend(fl)
    try:
        tracker, fin, files, skipped = analyze(paths, args, forced)
    except AnalysisError as e:
        print(f"[Error] {e}")
        sys.exit(2 if "device versions" in str(e) else 1)

    scope = SCOPES[args.scope]
    if scope and not tracker.parts[scope]:
        print(f"[Error] No {TEMP_LABEL[scope]} files in this lot - nothing to report for that area.")
        sys.exit(1)
    shown = [st for st in tracker.stages if scope is None or st.temperature == scope]
    print(f"\nDetected {len(tracker.stages)} Test Stage(s):")
    for stg in tracker.stages:
        print(f"  * {stg.display_name}: {len(stg.files)} STDF file(s)")
    print()
    for stg in shown:
        print_stage_table(stg)
    print_overall_waterfall(tracker, fin)

    if args.output_excel:
        extra = None
        if not args.no_file_sheets:
            import file_sheets
            print("Writing per-file datalog sheets ...")
            extra = lambda wb: file_sheets.add_file_sheets(wb, files, scope=scope)  # noqa: E731
        export_multi_stage_excel(args.output_excel, tracker, fin, files + skipped, skipped, scope=scope, extra_sheets=extra)


if __name__ == "__main__":
    main()
