#!/bin/bash
# Writes the user LaunchAgents for the Mac mini and loads the ones that are safe to load.
#
#   colima        brew service; the hummingbot-api containers restart with it (restart: unless-stopped)
#   condor        Condor in the foreground (no tmux), restarted when it dies
#   run           `jiaoyi run` in the mode of config/app.toml; staged in data/launchd/ and only
#                 installed into LaunchAgents (so it also starts at login) with --load-run
#   backup        daily tar of the decision log and module status
#   watchdog      every minute: `jiaoyi watchdog` sends a Telegram alert when the main loop's
#                 heartbeat (data/module_status.json) is older than 2 minutes; silent until the
#                 main loop has started once (data/runtime.json)
#
# Usage: scripts/install_launchd.sh [--load-run]   (idempotent: safe to run again)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$ROOT/data/logs"
UID_NUM="$(id -u)"
PATH_ENV="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p "$AGENTS" "$LOGS" "$ROOT/data/launchd"

plist() {
  local label="$1" workdir="$2" keepalive="$3" calendar="$4" interval="${INTERVAL:-}"
  shift 4
  local file="${PLIST_DIR:-$AGENTS}/$label.plist"
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0"><dict>'
    echo "  <key>Label</key><string>$label</string>"
    echo '  <key>ProgramArguments</key><array>'
    for a in "$@"; do echo "    <string>$a</string>"; done
    echo '  </array>'
    echo "  <key>WorkingDirectory</key><string>$workdir</string>"
    echo "  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$PATH_ENV</string><key>CONDOR_TELEMETRY</key><string>off</string></dict>"
    echo "  <key>StandardOutPath</key><string>$LOGS/$label.log</string>"
    echo "  <key>StandardErrorPath</key><string>$LOGS/$label.log</string>"
    if [ "$keepalive" = yes ]; then
      echo '  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>30</integer>'
    fi
    if [ -n "$calendar" ]; then
      echo "  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>$calendar</integer><key>Minute</key><integer>10</integer></dict>"
    fi
    if [ -n "$interval" ]; then
      echo "  <key>StartInterval</key><integer>$interval</integer>"
    fi
    echo '</dict></plist>'
  } >"$file"
  plutil -lint "$file" >/dev/null
  echo "$file"
}

load() {
  local label="$1" i err=""
  launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null || true
  sleep 3  # bootout is asynchronous; bootstrapping right away races with it (error 5)
  for i in 1 2 3; do
    if err="$(launchctl bootstrap "gui/$UID_NUM" "$AGENTS/$label.plist" 2>&1)"; then
      echo "loaded $label"
      return 0
    fi
    sleep 3
  done
  echo "bootstrap $label failed: $err" >&2
  # bootout did not take effect: the old definition is still registered, the new plist is NOT active
  if launchctl print "gui/$UID_NUM/$label" >/dev/null 2>&1; then
    echo "WARNING: $label still runs the OLD definition; run 'launchctl bootout gui/$UID_NUM/$label' and re-run this script" >&2
  fi
  return 1
}

FAILED=0

# Safe to re-run: `brew services start` fails (Bootstrap failed: 5) when colima is already loaded.
if launchctl print "gui/$UID_NUM/sh.brew.colima" >/dev/null 2>&1; then
  echo "colima already loaded, skipping"
else
  brew services start colima || { echo "colima start failed" >&2; FAILED=1; }
fi

plist com.jiaoyi.condor "$ROOT/vendor/condor" yes "" /bin/bash -lc "exec make run-fg"
PLIST_DIR="$ROOT/data/launchd" plist com.jiaoyi.run "$ROOT" yes "" "$ROOT/.venv/bin/jiaoyi" run
plist com.jiaoyi.backup "$ROOT" no 4 /bin/bash "$ROOT/scripts/backup_data.sh"
INTERVAL=60 plist com.jiaoyi.watchdog "$ROOT" no "" "$ROOT/.venv/bin/jiaoyi" watchdog

load com.jiaoyi.condor || FAILED=1
load com.jiaoyi.backup || FAILED=1
load com.jiaoyi.watchdog || FAILED=1
if [ "${1:-}" = "--load-run" ]; then
  cp "$ROOT/data/launchd/com.jiaoyi.run.plist" "$AGENTS/"
  load com.jiaoyi.run || FAILED=1
else
  echo "com.jiaoyi.run staged in data/launchd/, not installed (pass --load-run once the paper run is cleared)"
fi
exit "$FAILED"
