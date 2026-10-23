#!/usr/bin/env bash
# One command for the web control center: sets up on first run, then opens http://127.0.0.1:8765.
# Paper trading only. Ctrl-C stops the website; a bot started from the page keeps running.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || scripts/setup.sh
# The page saves settings here. It is not tracked by git, so updates never overwrite your settings.
[ -f config.web.toml ] || cp config.maker.toml config.web.toml
exec .venv/bin/python -m kbot --config config.web.toml dashboard --open "$@"
