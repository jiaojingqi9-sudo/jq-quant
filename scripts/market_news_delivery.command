#!/bin/zsh
cd "/Users/jiao/All here/news collector" || exit 1
export HOME="/Users/jiao"
export MARKET_NEWS_USER_AGENT="${MARKET_NEWS_USER_AGENT:-MarketNewsCollector/0.1 (delivery line)}"
export OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-/Users/jiao/.openclaw/openclaw.json}"
export OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-/Users/jiao/.openclaw}"
pkill -f "python3 -m market_news notify" >/dev/null 2>&1 || true
pkill -f "python3 -m market_news monitor" >/dev/null 2>&1 || true
python3 -m market_news notify --watch --interval 300
