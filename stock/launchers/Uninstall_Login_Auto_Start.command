#!/bin/zsh
set -euo pipefail

PLIST_PATH="$HOME/Library/LaunchAgents/com.jiao.taa_futu_watchdog.plist"
LEGACY_PLIST_PATH="$HOME/Library/LaunchAgents/com.jiao.taa_futu_auto_trader.plist"
launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl unload "$LEGACY_PLIST_PATH" >/dev/null 2>&1 || true
rm -f "$PLIST_PATH"
rm -f "$LEGACY_PLIST_PATH"
echo "Login auto-start removed."
