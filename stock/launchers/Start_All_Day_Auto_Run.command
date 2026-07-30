#!/bin/zsh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_DIR"

if [[ ! -x ".venv/bin/python" ]]; then
  osascript -e 'display alert "Missing .venv/bin/python" message "The local Python environment is missing. Ask Codex to reinstall the project dependencies." as critical'
  exit 1
fi

mkdir -p runtime
export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
WATCHDOG_PLIST="$HOME/Library/LaunchAgents/com.jiao.taa_futu_watchdog.plist"
if [[ -f "$WATCHDOG_PLIST" ]]; then
  launchctl load "$WATCHDOG_PLIST" >/dev/null 2>&1 || true
  echo "Watchdog launch agent loaded."
else
  nohup ".venv/bin/python" -m taa_futu.watchdog >> runtime/watchdog.log 2>&1 &
  echo "Watchdog started."
fi
