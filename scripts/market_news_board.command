#!/bin/zsh
cd "/Users/jiao/All here/news collector" || exit 1
export MARKET_NEWS_USER_AGENT="${MARKET_NEWS_USER_AGENT:-MarketNewsCollector/0.1 (web board)}"
python3 -m market_news monitor --dry-run-notify
dashboard_url="file:///Users/jiao/All%20here/news%20collector/reports/live/latest_dashboard.html?ts=$(date +%s)"
open "$dashboard_url"
python3 -m market_news monitor --watch --interval 300
