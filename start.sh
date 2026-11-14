#!/usr/bin/env bash
# macOS / Linux launcher: makes a private Python environment on first run, then starts the bot.
# Usage: ./start.sh            (or ./start.sh settings, ./start.sh verify logs/<run>, ...)
set -euo pipefail
cd "$(dirname "$0")"
PY=python3
command -v "$PY" >/dev/null 2>&1 || { echo "Install Python 3.10+ from https://www.python.org/downloads/"; exit 1; }
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up .venv ..."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
fi
exec .venv/bin/python run.py "$@"
