#!/bin/zsh
cd "/Users/jiao/All here/news collector" || exit 1

user_id="$(id -u)"
agents_dir="$HOME/Library/LaunchAgents"

for label in ai.codex.marketnews.collect ai.codex.marketnews.notify ai.codex.marketnews.health ai.codex.marketnews.reviewapi; do
  plist="$agents_dir/$label.plist"
  launchctl bootout "gui/$user_id" "$plist" >/dev/null 2>&1 || true
done

pkill -f "python3 -m market_news collect --watch --interval 300" >/dev/null 2>&1 || true
pkill -f "python3 -m market_news notify --watch --interval 300" >/dev/null 2>&1 || true
pkill -f "python3 -m market_news health --watch --interval 60" >/dev/null 2>&1 || true
pkill -f "python3 -m market_news monitor --watch --interval 300" >/dev/null 2>&1 || true
pkill -f "python3 -m market_news review-api" >/dev/null 2>&1 || true

echo "Market news stack stopped."
