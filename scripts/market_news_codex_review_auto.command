#!/bin/zsh
cd "/Users/jiao/All here/news collector" || exit 1

workdir="/Users/jiao/All here/news collector"
runtime_dir="$workdir/runtime"
log_dir="$runtime_dir/logs"
agents_dir="$HOME/Library/LaunchAgents"
python_bin="$(command -v python3)"
user_id="$(id -u)"

label="ai.codex.marketnews.codexreview"
plist_path="$agents_dir/$label.plist"

mkdir -p "$log_dir" "$agents_dir"

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
    <string>cd '$workdir' &amp;&amp; '$python_bin' -m market_news news-learning-codex-review --notify</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$workdir</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>/Users/jiao</string>
    <key>USER</key>
    <string>jiao</string>
    <key>LOGNAME</key>
    <string>jiao</string>
    <key>OPENCLAW_CONFIG_PATH</key>
    <string>/Users/jiao/.openclaw/openclaw.json</string>
    <key>OPENCLAW_STATE_DIR</key>
    <string>/Users/jiao/.openclaw</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>3600</integer>
  <key>StandardOutPath</key>
  <string>$log_dir/news_learning_codex_review.launchd.log</string>
  <key>StandardErrorPath</key>
  <string>$log_dir/news_learning_codex_review.launchd.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$user_id" "$plist_path" >/dev/null 2>&1 || true
pkill -f "python3 -m market_news news-learning-codex-review" >/dev/null 2>&1 || true

launchctl bootstrap "gui/$user_id" "$plist_path"
launchctl kickstart -k "gui/$user_id/$label"

sleep 2

{
  echo "news_learning_codex_review_label=$label"
  echo "news_learning_codex_review_plist=$plist_path"
  echo "status=$workdir/reports/live/news_learning_codex_review_status.json"
  echo "history=$workdir/reports/live/news_learning_codex_review_history.jsonl"
  echo "analysis=$workdir/reports/news_learning/news_learning_codex_analysis.md"
  echo "started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
} >"$runtime_dir/news_learning_codex_review_agent.txt"

echo "News learning Codex review automation started."
echo "label: $label"
echo "status: $workdir/reports/live/news_learning_codex_review_status.json"
echo "analysis: $workdir/reports/news_learning/news_learning_codex_analysis.md"
echo "logs: $log_dir/news_learning_codex_review.launchd.log"
