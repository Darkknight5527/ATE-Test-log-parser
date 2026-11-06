@echo off
REM Command line: drag STDF files / a lot folder onto this file. Add --profile and --traveller by editing the line below.
cd /d "%~dp0"
python -m ate_log_parser %*
pause
