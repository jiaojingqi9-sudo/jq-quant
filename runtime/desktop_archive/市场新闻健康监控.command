#!/bin/zsh
cd "/Users/jiao/All here/news collector" || exit 1
pkill -f "python3 -m market_news health --watch --interval 60" >/dev/null 2>&1 || true
python3 -m market_news health --watch --interval 60
