@echo off
REM Builds dist\ATE_Log_Parser\ATE_Log_Parser.exe on this PC (GitHub Actions does the same for releases)
cd /d "%~dp0"
python -m pip install --upgrade -r requirements.txt pyinstaller
if errorlevel 1 (echo pip failed & pause & exit /b 1)
python -m PyInstaller --noconfirm --clean --onedir --windowed --paths . --paths app --hidden-import gui --add-data "app\ui;ui" --name ATE_Log_Parser app\main.py
if errorlevel 1 (echo Build failed & pause & exit /b 1)
echo.
echo DONE: %~dp0dist\ATE_Log_Parser\ATE_Log_Parser.exe  (copy the whole ATE_Log_Parser folder)
pause
