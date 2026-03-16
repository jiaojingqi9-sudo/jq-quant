#!/bin/zsh
cd "/Users/jiao/All here/news collector" || exit 1
export MARKET_NEWS_USER_AGENT="${MARKET_NEWS_USER_AGENT:-MarketNewsCollector/0.1 (desktop console)}"
pkill -f "python3 -m market_news collect --watch --interval 300" >/dev/null 2>&1 || true
pkill -f "python3 -m market_news monitor --watch --interval 300" >/dev/null 2>&1 || true
python3 -m market_news collect --watch --interval 300
