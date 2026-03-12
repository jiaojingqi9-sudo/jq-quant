#!/bin/zsh
cd "/Users/jiao/All here/news collector" || exit 1
export MARKET_NEWS_USER_AGENT="${MARKET_NEWS_USER_AGENT:-MarketNewsCollector/0.1 (desktop console)}"
python3 -m market_news monitor --watch --interval 300
