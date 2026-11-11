# Lot Yield Viewer

A Windows desktop app for semiconductor final-test datalogs (STDF V4). Drop in a lot's STDF files and you get,
without writing a report first:

- **Detailed Info** - every file at a glance (tests run, unique chips, retested, pass, fail, yield, lot, program,
  tester), per-test statistics (limits, mean, std dev, Cp, Cpk) and a **test-limits table** that shows where Hot or
  Cold limits differ from Room.
- **Charts** - trend of one test across all runs, and correlation / delta of the *same physical chip* between two
  insertions (e.g. Room vs Hot).
- **Lot Traveller** - the numbers to type into the lot traveller, per test block (start / stop, lot size, pass, bins),
  with copy buttons.
- **Production Lots** - saved lots (just file paths, the files stay where they are), "check folder for new files",
  and a multi-stage recovery waterfall.
- **Report** - an Excel workbook: final lot summary, waterfall per insertion, files, parts ledger, exceptions, and
  (optional) **one datalog sheet per STDF file** with yield / fail summary, reject pareto, Cpk, statistics and every
  test value of every run.

## How chips are counted

A lot is tested at several insertions (Room, Post-Bake, Hot, Cold), each with first runs and retests, and the same
chip can be inserted again under a new Part ID. Adding up the files therefore counts chips twice. The viewer counts
**physical chips**:

- a chip retested on the following rows of one file is one chip (last result wins);
- chips are followed from file to file by the **fuse ID** the test program burns into them (read-back value and
  per-bit fuse tests), with post-trim measurements as a tie-break when two chips share an ID;
- chips that failed before the fuse was burned carry no ID; they are matched as a pool against later retests;
- a chip is good only if it passed every insertion the lot was screened at;
- **chips off the test flow** are listed: passed an insertion but never tested at the next one (on hold), skipped an
  insertion but showed up at a later one, or failed an insertion but were still tested further.

## Device profile

Which tests carry the fuse ID is different for every product, so it is **not in the code**. It lives in a small JSON
file on your PC (see [`examples/example_profile.json`](examples/example_profile.json)) that you import once with the
**Profile** button (top right). Profiles are stored in `%APPDATA%\lot_yield_app\profiles`. Without a profile the app
still works, but chips are only followed by Part ID inside a file.

| key | meaning |
|---|---|
| `readback_test` | part of the name of the test whose value is the fuse word read back from the chip |
| `fuse_bits_prefix`, `fuse_bit_regex`, `fuse_bank`, `blown_above` | per-bit fuse tests: name start, pattern with a `bit` group (and optional `bank`), which bank, resistance above which a bit is blown |
| `match_tests` | name starts of post-trim tests used to tell apart two chips with the same fuse word |
| `burn_fail_bins`, `burn_check_bins` | soft-bin name words for "failed while burning the fuse" / "check before burning" |
| `device_regex` | device version from program / file name (files are analysed per version) |
| `lot_regex`, `lot_prefix` | lot number from lot ID or folder name |
| `exclude_regex` | file names to leave out (sanity-check files are always left out) |

## Use

**App (no Python needed):** download `Lot_Yield_Viewer-<version>-windows.zip` from
[Releases](../../releases/latest), unzip, run `Lot_Yield_Viewer.exe`. If Windows says "Windows protected your PC",
click *More info* -> *Run anyway* (the app is not code-signed).

**From source:**

```
pip install -r requirements.txt
python lot_yield_app.py                                        # the app
python stdf_lot_yield_summary.py -i <lot folder> -o report.xlsx --profile my_profile.json   # command line
python get_limits.py <file or folder> [test number or name]    # print test limits
Build_Lot_Yield_Viewer_exe.bat                                 # build the .exe yourself
```

The command line uses the profile last chosen in the app when `--profile` is not given.

## Development

- `stdf_lot_yield_summary.py` - STDF reading and the chip counting (also the command line)
- `viewer_core.py` - data behind the tabs, reading cache, saved lots
- `lot_yield_app.py` + `ui/` - the window (pywebview / Edge WebView2, Chart.js)
- `profiles.py` - device profiles
- `file_sheets.py`, `datalog_reader.py`, `datalog_sheets.py` - per-file datalog sheets
- `tests/` - pytest on synthetic STDF files written by `tests/stdf_writer.py`

Every push builds the app on GitHub Actions (data-safety check, tests, PyInstaller); a `v*` tag publishes a release.

## History

Up to v0.3.1 this repo held **ATE Log Parser** (STDF lot report + lot traveller fill-in). Its releases are still
on the Releases page and its code is in the git history; its per-file datalog sheets, device profiles, test-flow
checks, tests and build pipeline now live in this app.

## Data policy

Code only - no datalogs, reports, profiles or company / product names. See [DATA_POLICY.md](DATA_POLICY.md);
enforced by `.gitignore` and `tools/check_repo_clean.py` (install the pre-commit hook with `tools/install_hooks.bat`).

Fonts: Outfit (SIL Open Font License, `ui/fonts/OFL-Outfit.txt`). Charts: Chart.js (MIT).
