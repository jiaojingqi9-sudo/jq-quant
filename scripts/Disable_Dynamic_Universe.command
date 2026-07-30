#!/bin/zsh
# 双击关闭动态 universe，回到原来的静态 tech_universe_cn_hk.json。

set -e

ENV_FILE="$HOME/.market_news/futu_env"
if [ -f "$ENV_FILE" ]; then
  grep -v "^export MARKET_NEWS_TECH_UNIVERSE_DYNAMIC=" "$ENV_FILE" > "$ENV_FILE.tmp" || true
  mv "$ENV_FILE.tmp" "$ENV_FILE"
  # If futu_env is now empty (no other vars left), remove it.
  if [ ! -s "$ENV_FILE" ]; then
    rm "$ENV_FILE"
  fi
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$SCRIPT_DIR/market_news_stack.command" 2>/dev/null || true

osascript -e 'display dialog "✅ 已恢复使用静态 universe\n（动态 universe 文件仍保留在 config/ 下，下次开启可直接复用）" buttons {"知道了"} default button "知道了" with title "Dynamic Universe Disabled"' >/dev/null 2>&1 || true
