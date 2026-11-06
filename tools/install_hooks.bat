@echo off
REM Installs the pre-commit check into this repo (run once per clone).
cd /d "%~dp0\.."
if not exist .git (
  echo Run this from inside the ate-test-log-parser repo.
  pause & exit /b 1
)
(
  echo #!/bin/sh
  echo python tools/check_repo_clean.py --staged ^|^| exit 1
) > .git\hooks\pre-commit
echo Pre-commit check installed. Every "git commit" now runs tools\check_repo_clean.py first.
if not exist .private_denylist.txt (
  echo # one word per line - company, customer, device, tester host, people names > .private_denylist.txt
  echo Created .private_denylist.txt - open it and add your private words now.
  notepad .private_denylist.txt
)
pause
