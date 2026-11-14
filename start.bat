@echo off
rem Windows launcher: makes a private Python environment on first run, then starts the bot.
rem Usage: double-click, or  start.bat settings  /  start.bat verify logs\<run>
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up .venv ...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if errorlevel 1 (
    echo Install Python 3.10+ from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
)
".venv\Scripts\python.exe" run.py %*
pause
