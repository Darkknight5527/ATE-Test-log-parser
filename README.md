# ATE Test Log Parser

Windows desktop tool for semiconductor test datalogs (STDF V4, e.g. Advantest V93000).
Give it the STDF files of one lot and it produces:

1. **Lot report** (Excel)
   - **Summary** - one row per file: stage (Room / Hot / Cold), fresh or retest, parts, pass / fail, what is in the file, tester cross-check
   - **Lot Summary** - every physical part counted **once**: distinct parts and good parts per stage, versus simply adding up files
   - **Exceptions** - parts that had already passed and were tested again, extra parts in retest files, parts never seen before
   - **Part Journeys** - every part's path through the lot, e.g. `Room:F > Room:P > Hot:P > Cold:P`
   - **Unit Ledger**, **Files**, **Method**
   - one sheet per STDF in the classic datalog-viewer layout (yield, reject pareto, Cpk, statistics, all unit values)
2. **Lot traveller fill-in** (optional) - a copy of your traveller with an extra *STDF Fill-in* sheet:
   the values for every test-sheet block (start/stop, lot size, pass/fail, bins), a check against what is already filled in,
   and lot totals without duplicates. The traveller itself is never modified.

## How parts are recognised
Part IDs in STDF restart in every file, so they cannot link files. Parts are linked by an **ID word programmed into the part**
(read back at the start of the test flow) plus **comparison measurements** when several parts share a word. Parts that never got
programmed (e.g. contact fails) are matched by count against earlier fails. Which tests to use is set in a **device profile**
(see `examples/example_profile.json`) - profiles stay on your PC and are never committed.

## Download and use (no Python needed)
1. Open **Releases** (right side of this page) and download `ATE_Log_Parser-<version>-windows.zip`.
2. Unzip it and run `ATE_Log_Parser.exe` (Windows may warn because the app is not code-signed: *More info -> Run anyway*).
3. **Import profile...** once (your device profile JSON), drop the lot's STDF files or folder on the window, optionally pick the traveller, **Generate report**.

The window uses Microsoft Edge WebView2, which is already on Windows 10/11. If it is missing, the app opens a plain fallback window with the same functions.

## Run from source
```
pip install -r requirements.txt
python app/main.py                                    # window (pywebview)
python app/gui.py                                     # plain fallback window (tkinter)
# UI preview in a browser with demo data: open app/ui/index.html?demo
python -m ate_log_parser <lot folder> --profile my.json --traveller traveller.xlsx   # command line
python -m pytest tests                                # tests on generated (fake) STDF files
```

## Data policy
Code only - no tester data, reports, profiles or company information. See [DATA_POLICY.md](DATA_POLICY.md).
