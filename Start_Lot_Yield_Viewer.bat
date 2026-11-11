@echo off
REM Starts the Lot Yield Viewer (first time: installs openpyxl and pywebview).
cd /d "%~dp0"
python -c "import openpyxl, webview" 2>nul || python -m pip install --quiet openpyxl pywebview
start "" pythonw "%~dp0lot_yield_app.py"
