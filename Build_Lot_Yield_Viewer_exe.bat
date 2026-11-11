@echo off
REM Builds a stand-alone app folder (no Python needed afterwards): dist\Lot_Yield_Viewer\Lot_Yield_Viewer.exe
cd /d "%~dp0"
python -m pip install --quiet openpyxl pywebview pyinstaller
python -m PyInstaller --noconfirm --onedir --windowed --name Lot_Yield_Viewer --add-data "ui;ui" --hidden-import stdf_lot_yield_summary --hidden-import viewer_core --hidden-import profiles --hidden-import file_sheets --hidden-import datalog_reader --hidden-import datalog_sheets lot_yield_app.py
echo.
echo Done: dist\Lot_Yield_Viewer\Lot_Yield_Viewer.exe
pause
