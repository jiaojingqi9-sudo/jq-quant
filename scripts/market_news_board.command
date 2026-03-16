#!/bin/zsh
cd "/Users/jiao/All here/news collector" || exit 1
export MARKET_NEWS_USER_AGENT="${MARKET_NEWS_USER_AGENT:-MarketNewsCollector/0.1 (web board)}"
pkill -f "python3 -m market_news collect --watch --interval 300" >/dev/null 2>&1 || true
pkill -f "python3 -m market_news monitor --watch --interval 300" >/dev/null 2>&1 || true
python3 -m market_news collect
dashboard_url="file:///Users/jiao/All%20here/news%20collector/reports/live/latest_dashboard.html?ts=$(date +%s)"
open "$dashboard_url"
python3 -m market_news collect --watch --interval 300
