#!/bin/zsh
cd "/Users/jiao/All here/news collector" || exit 1

workdir="/Users/jiao/All here/news collector"
runtime_dir="$workdir/runtime"
log_dir="$runtime_dir/logs"
agents_dir="$HOME/Library/LaunchAgents"
python_bin="$(command -v python3)"
user_id="$(id -u)"

mkdir -p "$log_dir" "$agents_dir"

write_agent() {
  local label="$1"
  local interval="$2"
  local command_line="$3"
  local stdout_path="$4"
  local stderr_path="$5"
  local plist_path="$6"

  cat >"$plist_path" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>cd '$workdir' &amp;&amp; $command_line</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$workdir</string>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>$interval</integer>
  <key>StandardOutPath</key>
  <string>$stdout_path</string>
  <key>StandardErrorPath</key>
  <string>$stderr_path</string>
</dict>
</plist>
EOF
}

write_keepalive_agent() {
  local label="$1"
  local command_line="$2"
  local stdout_path="$3"
  local stderr_path="$4"
  local plist_path="$5"

  cat >"$plist_path" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>cd '$workdir' &amp;&amp; $command_line</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$workdir</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$stdout_path</string>
  <key>StandardErrorPath</key>
  <string>$stderr_path</string>
</dict>
</plist>
EOF
}

collect_label="ai.codex.marketnews.collect"
notify_label="ai.codex.marketnews.notify"
health_label="ai.codex.marketnews.health"
review_api_label="ai.codex.marketnews.reviewapi"

collect_plist="$agents_dir/$collect_label.plist"
notify_plist="$agents_dir/$notify_label.plist"
health_plist="$agents_dir/$health_label.plist"
review_api_plist="$agents_dir/$review_api_label.plist"

write_agent \
  "$collect_label" \
  "300" \
  "export MARKET_NEWS_USER_AGENT='MarketNewsCollector/0.1 (launchd collect)'; '$python_bin' -m market_news collect" \
  "$log_dir/collect.launchd.log" \
  "$log_dir/collect.launchd.err.log" \
  "$collect_plist"

write_agent \
  "$notify_label" \
  "300" \
  "export MARKET_NEWS_USER_AGENT='MarketNewsCollector/0.1 (launchd notify)'; '$python_bin' -m market_news notify" \
  "$log_dir/notify.launchd.log" \
  "$log_dir/notify.launchd.err.log" \
  "$notify_plist"

write_agent \
  "$health_label" \
  "60" \
  "'$python_bin' -m market_news health" \
  "$log_dir/health.launchd.log" \
  "$log_dir/health.launchd.err.log" \
  "$health_plist"

write_keepalive_agent \
  "$review_api_label" \
  "'$python_bin' -m market_news review-api" \
  "$log_dir/review_api.launchd.log" \
  "$log_dir/review_api.launchd.err.log" \
  "$review_api_plist"

pkill -f "python3 -m market_news collect --watch --interval 300" >/dev/null 2>&1 || true
pkill -f "python3 -m market_news notify --watch --interval 300" >/dev/null 2>&1 || true
pkill -f "python3 -m market_news health --watch --interval 60" >/dev/null 2>&1 || true
pkill -f "python3 -m market_news monitor --watch --interval 300" >/dev/null 2>&1 || true

for plist in "$collect_plist" "$notify_plist" "$health_plist" "$review_api_plist"; do
  launchctl bootout "gui/$user_id" "$plist" >/dev/null 2>&1 || true
done

launchctl bootstrap "gui/$user_id" "$collect_plist"
launchctl bootstrap "gui/$user_id" "$notify_plist"
launchctl bootstrap "gui/$user_id" "$health_plist"
launchctl bootstrap "gui/$user_id" "$review_api_plist"

launchctl kickstart -k "gui/$user_id/$collect_label"
launchctl kickstart -k "gui/$user_id/$notify_label"
launchctl kickstart -k "gui/$user_id/$health_label"
launchctl kickstart -k "gui/$user_id/$review_api_label"

sleep 2

cat >"$runtime_dir/stack_agents.txt" <<EOF
collect_label=$collect_label
notify_label=$notify_label
health_label=$health_label
review_api_label=$review_api_label
collect_plist=$collect_plist
notify_plist=$notify_plist
health_plist=$health_plist
review_api_plist=$review_api_plist
started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
EOF

dashboard_url="file:///Users/jiao/All%20here/news%20collector/reports/live/latest_dashboard.html?ts=$(date +%s)"
open "$dashboard_url"

echo "Market news stack started through launchd."
echo "collect label: $collect_label"
echo "notify label: $notify_label"
echo "health label: $health_label"
echo "review api label: $review_api_label"
echo "dashboard: $dashboard_url"
echo "logs: $log_dir"
