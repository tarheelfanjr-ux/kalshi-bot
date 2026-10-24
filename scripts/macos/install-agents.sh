#!/usr/bin/env bash
# Keep the paper bot and the recorder running on a Mac: launchd starts them at login, restarts them
# if they exit, and caffeinate keeps the Mac awake while they run on power. Paper mode only.
#   scripts/macos/install-agents.sh            install and start both
#   scripts/macos/install-agents.sh remove     stop and remove both
set -euo pipefail
# shortcut: a folder path containing & or < breaks the generated plist. Keep the repo in a plain path.
cd "$(dirname "$0")/../.."
REPO=$(pwd)
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
[ -x .venv/bin/python ] || { echo "run scripts/setup.sh first"; exit 1; }
case "$REPO" in   # macOS privacy rules stop background jobs from reading these folders
  "$HOME/Documents"*|"$HOME/Desktop"*|"$HOME/Downloads"*|*"Mobile Documents"*|*CloudStorage*)
    echo "Move the repo out of $REPO first, for example to ~/kalshi-bot (docs/HOSTING.md)."; exit 1;;
esac
mkdir -p "$AGENTS" logs

agent() {   # label, log name, kbot arguments...
  local label=$1 logname=$2; shift 2
  local args=""
  for a in /usr/bin/caffeinate -s "$REPO/.venv/bin/python" -m kbot "$@"; do args+="    <string>$a</string>"$'\n'; done
  cat > "$AGENTS/$label.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>ProgramArguments</key>
  <array>
$args  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$REPO/logs/$logname.log</string>
  <key>StandardErrorPath</key><string>$REPO/logs/$logname.log</string>
</dict>
</plist>
PLIST
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$AGENTS/$label.plist"
  echo "started $label (log: logs/$logname.log)"
}

if [ "${1:-}" = remove ]; then
  for label in com.kbot.paper com.kbot.recorder; do
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    rm -f "$AGENTS/$label.plist"
    echo "removed $label"
  done
  exit 0
fi
agent com.kbot.paper paper-maker --config config.maker.toml --log-level WARNING run
agent com.kbot.recorder recorder --log-level WARNING record
