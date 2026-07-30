#!/bin/zsh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_DIR"

WATCHDOG_PLIST="$HOME/Library/LaunchAgents/com.jiao.taa_futu_watchdog.plist"
launchctl unload "$WATCHDOG_PLIST" >/dev/null 2>&1 || true

for PID_FILE in "runtime/watchdog.pid" "runtime/auto_trader.pid"; do
  if [[ -f "$PID_FILE" ]]; then
    PID="$(cat "$PID_FILE")"
    kill "$PID" 2>/dev/null || true
  fi
done

echo "Stop signal sent to watchdog and auto trader."
