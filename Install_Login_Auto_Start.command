#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/com.jiao.taa_futu_watchdog.plist"
LEGACY_PLIST_PATH="$PLIST_DIR/com.jiao.taa_futu_auto_trader.plist"
LOG_PATH="$SCRIPT_DIR/runtime/watchdog.log"

mkdir -p "$PLIST_DIR"
mkdir -p "$SCRIPT_DIR/runtime"

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.jiao.taa_futu_watchdog</string>
  <key>ProgramArguments</key>
  <array>
    <string>$SCRIPT_DIR/.venv/bin/python</string>
    <string>-m</string>
    <string>taa_futu.watchdog</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>$SCRIPT_DIR/src</string>
  </dict>
  <key>WorkingDirectory</key>
  <string>$SCRIPT_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG_PATH</string>
  <key>StandardErrorPath</key>
  <string>$LOG_PATH</string>
</dict>
</plist>
PLIST

launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl unload "$LEGACY_PLIST_PATH" >/dev/null 2>&1 || true
rm -f "$LEGACY_PLIST_PATH"
launchctl load "$PLIST_PATH"
echo "Login auto-start installed and loaded with watchdog."
