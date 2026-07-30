#!/usr/bin/env bash
set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR" || exit 1

RUNTIME_DIR="$ROOT_DIR/runtime/crypto_ofim"
LOG_FILE="$RUNTIME_DIR/overnight_monitor.log"
PID_FILE="$RUNTIME_DIR/overnight_monitor.pid"
mkdir -p "$RUNTIME_DIR"

echo "$$" > "$PID_FILE"
echo "[$(date -u +%FT%TZ)] monitor_start pid=$$ duration_hours=${1:-12}" >> "$LOG_FILE"

duration_hours="${1:-12}"
end_ts=$(( $(date +%s) + duration_hours * 3600 ))

while [ "$(date +%s)" -lt "$end_ts" ]; do
  ts="$(date -u +%FT%TZ)"
  http="$(curl -Is --max-time 5 http://localhost:8503/ 2>/dev/null | head -n 1 | tr -d '\r')"
  watchdog="$(PYTHONPATH=src .venv/bin/python -m taa_futu.cli crypto-ofim-watchdog-status 2>&1 | tr '\n' ' ' | sed -E 's/[[:space:]]+/ /g')"
  state="$(PYTHONPATH=src .venv/bin/python -m taa_futu.cli crypto-ofim-status 2>&1 | sed -n '1,12p' | tr '\n' ' ' | sed -E 's/[[:space:]]+/ /g')"
  echo "[$ts] http=${http:-NO_HTTP} watchdog=$watchdog state=$state" >> "$LOG_FILE"
  sleep 300
done

echo "[$(date -u +%FT%TZ)] monitor_done" >> "$LOG_FILE"
