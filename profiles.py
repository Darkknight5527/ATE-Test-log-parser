"""Device profiles: everything that is specific to one product (test names, name patterns) lives in a small JSON file
on the user's PC, never in the code. Without a profile the viewer still works, but parts can only be followed by
Part ID inside a file (no fuse ID), and files are not split by device version.

Profile keys (all optional):
  name               shown in the app
  readback_test      part of the name of the test whose value is the fuse word read back from the chip
  fuse_bits_prefix   start of the names of the per-bit fuse tests (one test per bit, value = resistance)
  fuse_bit_regex     regex on those test names: group 'bit' = bit number, optional group 'bank' = bank letter
  fuse_bank          only bits of this bank are used ('' = all)
  blown_above        a bit counts as blown when its value is above this
  match_tests        test-name prefixes whose values tell two parts with the same fuse word apart
  burn_fail_bins     soft-bin name words that mean "failed while burning the fuse" (chip is programmed, no word logged)
  burn_check_bins    soft-bin name words that look like the above but are the read-back BEFORE burning
  device_regex       regex on program / file name that gives the device version (files are analysed per version)
  lot_regex          regex with one group = lot number, e.g. "PROD0*(\\d+)"; lot_prefix is put in front of the number
  lot_prefix
  exclude_regex      file names to leave out (set-up files, board checks ...)
  hot_regex / cold_regex / postbake_regex   optional overrides of how the insertion is read from the file name
  continuity_suite   name start of the continuity tests (listed apart in the per-file sheets)
"""
import glob
import hashlib
import json
import os
import re
import shutil

DEFAULT = {
    "name": "Generic (no fuse ID)",
    "readback_test": "",
    "fuse_bits_prefix": "",
    "fuse_bit_regex": r"(?:BANK(?P<bank>[A-Z])_)?\w*?(?P<un>UN)?BLOWN_BIT(?P<bit>\d+)",
    "fuse_bank": "",
    "blown_above": 1000.0,
    "match_tests": [],
    "burn_fail_bins": [],
    "burn_check_bins": [],
    "device_regex": "",
    "lot_regex": "",
    "lot_prefix": "",
    "exclude_regex": r"FIFO|BOARD",
    "hot_regex": "",
    "cold_regex": "",
    "postbake_regex": "",
    "continuity_suite": "CONTINUITY",
}

_current = dict(DEFAULT)


def current():
    return dict(_current)


def merged(p):
    out = dict(DEFAULT)
    out.update({k: v for k, v in (p or {}).items() if v is not None})
    out["match_tests"] = list(out.get("match_tests") or [])
    return out


def signature(p=None):
    """Short hash of everything that changes how a file is read (used in the cache key)."""
    p = merged(p if p is not None else _current)
    keys = ("readback_test", "fuse_bits_prefix", "fuse_bit_regex", "fuse_bank", "blown_above", "match_tests")
    return hashlib.sha1(json.dumps([p[k] for k in keys], sort_keys=True).encode()).hexdigest()[:10]


def check(p):
    """Error text for a profile that cannot be used, else ''."""
    if not isinstance(p, dict):
        return "not a JSON object"
    for k in ("fuse_bit_regex", "device_regex", "lot_regex", "exclude_regex", "hot_regex", "cold_regex", "postbake_regex"):
        v = p.get(k)
        if v:
            try:
                re.compile(v)
            except re.error as e:
                return "%s is not a valid pattern (%s)" % (k, e)
    if p.get("fuse_bit_regex") and "(?P<bit>" not in p["fuse_bit_regex"]:
        return "fuse_bit_regex needs a group named 'bit'"
    return ""


def apply(p, module=None):
    """Make profile p the active one (in this process). module: the engine module object when it runs as a script."""
    if module is None:
        import stdf_lot_yield_summary as module
    S = module
    p = merged(p)
    _current.clear()
    _current.update(p)
    S.DEFAULT_FINGERPRINT = p["readback_test"]
    S.DEFAULT_FUSE_BITS = p["fuse_bits_prefix"]
    S.BIT_RX = re.compile(p["fuse_bit_regex"]) if p["fuse_bit_regex"] else None
    S.FUSE_BANK = p["fuse_bank"] or ""
    S.BLOWN_OHMS = float(p["blown_above"])
    S.POST_TRIM_PREFIXES = tuple(p["match_tests"])
    S.BURN_FAIL_KEYS = tuple(k.upper() for k in p["burn_fail_bins"])
    S.BURN_CHECK_KEYS = tuple(k.upper() for k in p["burn_check_bins"])
    S.DEVICE_RX = re.compile(p["device_regex"]) if p["device_regex"] else None
    S.LOT_RX = re.compile(p["lot_regex"], re.I) if p["lot_regex"] else None
    S.LOT_PREFIX = p["lot_prefix"] or ""
    S.DEFAULT_EXCLUDE = p["exclude_regex"] or ""
    S.HOT_RX = re.compile(p["hot_regex"], re.I) if p["hot_regex"] else None
    S.COLD_RX = re.compile(p["cold_regex"], re.I) if p["cold_regex"] else None
    S.POSTBAKE_OVERRIDE_RX = re.compile(p["postbake_regex"], re.I) if p["postbake_regex"] else None
    try:
        import datalog_sheets
        datalog_sheets.CONTI[0] = p.get("continuity_suite") or ""
    except ImportError:  # noqa
        pass
    return p


# ---------------------------------------------------------------------------------------------------- storage
def folder(app_dir):
    d = os.path.join(app_dir, "profiles")
    os.makedirs(d, exist_ok=True)
    return d


def load(path):
    with open(path, encoding="utf-8") as fh:
        p = json.load(fh)
    err = check(p)
    if err:
        raise ValueError(err)
    return merged(p)


def list_profiles(app_dir):
    """{name: path} - the generic profile has path ''."""
    out = {DEFAULT["name"]: ""}
    for f in sorted(glob.glob(os.path.join(folder(app_dir), "*.json"))):
        try:
            out[load(f)["name"]] = f
        except Exception:  # noqa - broken file: skip it
            pass
    return out


def install(app_dir, src):
    """Copy a profile JSON into the app's profile folder; returns its name."""
    p = load(src)
    if p["name"] == DEFAULT["name"]:
        raise ValueError("give the profile its own name (\"name\": ...)")
    dst = os.path.join(folder(app_dir), re.sub(r"[^\w\-]+", "_", p["name"]).strip("_")[:60] + ".json")
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copyfile(src, dst)
    return p["name"]


def default_app_dir():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "lot_yield_app")


def saved():
    """The profile last chosen in the desktop app ({} if none) - so the command line counts the same way."""
    try:
        d = default_app_dir()
        with open(os.path.join(d, "app_state.json"), encoding="utf-8") as fh:
            name = json.load(fh).get("profile", "")
        return by_name(d, name) if name else {}
    except Exception:  # noqa
        return {}


def by_name(app_dir, name):
    path = list_profiles(app_dir).get(name)
    return load(path) if path else merged({})
