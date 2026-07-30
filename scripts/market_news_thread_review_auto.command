#!/bin/zsh
cd "/Users/jiao/All here/news collector" || exit 1

workdir="/Users/jiao/All here/news collector"
runtime_dir="$workdir/runtime"
log_dir="$runtime_dir/logs"
agents_dir="$HOME/Library/LaunchAgents"
python_bin="$(command -v python3)"
codex_bin="/Applications/Codex.app/Contents/Resources/codex"
user_id="$(id -u)"

label="ai.codex.marketnews.threadreview"
plist_path="$agents_dir/$label.plist"
thread_id="019ce1c2-34f3-7e60-b324-bf7422ef1506"

mkdir -p "$log_dir" "$agents_dir" "$workdir/reports/news_learning" "$workdir/reports/live"

runner="$runtime_dir/news_learning_thread_review_runner.sh"

cat >"$runner" <<'EOF'
#!/bin/zsh
set -euo pipefail

workdir="/Users/jiao/All here/news collector"
python_bin="/opt/anaconda3/bin/python3"
codex_bin="/Applications/Codex.app/Contents/Resources/codex"
thread_id="019ce1c2-34f3-7e60-b324-bf7422ef1506"
analysis_path="$workdir/reports/news_learning/news_learning_thread_review_last.md"
status_path="$workdir/reports/live/news_learning_thread_review_status.json"
history_path="$workdir/reports/live/news_learning_thread_review_history.jsonl"

cd "$workdir"
"$python_bin" -m market_news news-learning-auto --no-copy >/dev/null

prompt='你是新闻收集系统的自动审阅助手。请直接在这个聊天框里给用户一条简洁中文审阅，不要改代码，不要改 live/news production 配置，不要改股票系统，不要改 crypto 系统。

请读取并审阅：
- /Users/jiao/All here/news collector/reports/news_learning/news_learning_codex_handoff.md
- /Users/jiao/All here/news collector/reports/news_learning/news_learning_review_packet.md
- /Users/jiao/All here/news collector/reports/news_learning/news_learning_review_packet.json

审阅重点：
- 来源质量：哪些来源该继续观察、升权、降权、交叉验证或拉黑。
- 主题质量：哪些主题真有预测价值，哪些像噪声。
- 数据质量：duplicate/stale/refuted/noise、source_diversity、entity/topic coverage。
- 候选建议：只列值得用户下一步确认的 candidate_id。

如果没有值得行动的建议，请只回复：
新闻学习审阅：暂不建议改代码或采集策略。
原因：<一句话>

如果值得用户看，请回复：
新闻学习审阅：建议用户确认是否变更。
最值得看的问题：
1. <证据>
建议动作：
1. <candidate_id> <action> <target>：<为什么>
如果用户同意，建议下一条指令：
<一句中文指令>

严格控制在 900 字以内。'

set +e
"$codex_bin" -a never exec resume "$thread_id" "$prompt" -o "$analysis_path"
code="$?"
set -e

timestamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
escaped_analysis_path="${analysis_path//\\/\\\\}"
escaped_analysis_path="${escaped_analysis_path//\"/\\\"}"
payload="{\"timestamp\":\"$timestamp\",\"overall_status\":\"$([ "$code" = "0" ] && echo ok || echo error)\",\"thread_id\":\"$thread_id\",\"analysis\":\"$escaped_analysis_path\",\"returncode\":$code}"
printf '%s\n' "$payload" > "$status_path"
printf '%s\n' "$payload" >> "$history_path"
exit "$code"
EOF

chmod +x "$runner"

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
    <string>$runner</string>
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
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>3600</integer>
  <key>StandardOutPath</key>
  <string>$log_dir/news_learning_thread_review.launchd.log</string>
  <key>StandardErrorPath</key>
  <string>$log_dir/news_learning_thread_review.launchd.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$user_id" "$plist_path" >/dev/null 2>&1 || true
pkill -f "news_learning_thread_review_runner.sh" >/dev/null 2>&1 || true

launchctl bootstrap "gui/$user_id" "$plist_path"
launchctl kickstart -k "gui/$user_id/$label"

{
  echo "news_learning_thread_review_label=$label"
  echo "news_learning_thread_review_plist=$plist_path"
  echo "thread_id=$thread_id"
  echo "runner=$runner"
  echo "status=$workdir/reports/live/news_learning_thread_review_status.json"
  echo "analysis=$workdir/reports/news_learning/news_learning_thread_review_last.md"
  echo "started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
} >"$runtime_dir/news_learning_thread_review_agent.txt"

echo "News learning thread review automation started."
echo "label: $label"
echo "thread: $thread_id"
echo "status: $workdir/reports/live/news_learning_thread_review_status.json"
