#!/bin/zsh
cd "/Users/jiao/All here/news collector" || exit 1

user_id="$(id -u)"
label="ai.codex.marketnews.threadreview"
plist_path="$HOME/Library/LaunchAgents/$label.plist"

launchctl bootout "gui/$user_id" "$plist_path" >/dev/null 2>&1 || true
pkill -f "news_learning_thread_review_runner.sh" >/dev/null 2>&1 || true

echo "News learning thread review automation stopped."
echo "label: $label"
