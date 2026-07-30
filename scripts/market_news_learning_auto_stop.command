#!/bin/zsh
cd "/Users/jiao/All here/news collector" || exit 1

user_id="$(id -u)"
label="ai.codex.marketnews.newslearning"
plist_path="$HOME/Library/LaunchAgents/$label.plist"

launchctl bootout "gui/$user_id" "$plist_path" >/dev/null 2>&1 || true
pkill -f "python3 -m market_news news-learning-auto" >/dev/null 2>&1 || true

echo "News learning automation stopped."
echo "label: $label"
