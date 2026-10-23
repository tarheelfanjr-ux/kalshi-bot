#!/usr/bin/env bash
# One-command setup on macOS or Linux: virtualenv, dependencies, local config files, full test run.
# Safe to run again. It never asks for or reads API keys.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "Python 3.11 or newer is needed (found $("$PY" -V 2>&1))"; exit 1; }
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt
[ -f config.toml ] || cp config.example.toml config.toml
[ -f .env ] || { cp .env.example .env; chmod 600 .env; }
mkdir -p data reports logs
.venv/bin/python -m pytest -q
cat <<'MSG'

Setup done. Real-money trading is off and stays off until you arm it yourself (docs/GO_LIVE.md).

Paper trading, maker strategy (no key needed):
  .venv/bin/python -m kbot --config config.maker.toml run       # Ctrl-C stops it
  .venv/bin/python -m kbot --config config.maker.toml status    # in a second terminal
Keep it running in the background with a log:
  nohup .venv/bin/python -m kbot --config config.maker.toml run >> logs/paper-maker.log 2>&1 &
MSG
