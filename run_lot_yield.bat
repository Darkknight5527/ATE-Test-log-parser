@echo off
REM Drag and drop the STDF files (or the lot folder) of ONE lot onto this file.
REM The report is saved next to the first dropped item as Lot_Yield_Report_<date>_<time>.xlsx
if "%~1"=="" (
  echo Drag the STDF files or the lot folder onto this .bat file.
  pause
  exit /b 1
)
set "STAMP=%date:~-4%%date:~3,2%%date:~0,2%_%time:~0,2%%time:~3,2%"
set "STAMP=%STAMP: =0%"
python "%~dp0stdf_lot_yield_summary.py" -i %* -o "%~dp1Lot_Yield_Report_%STAMP%.xlsx"
pause
