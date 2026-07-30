#!/bin/zsh
# 双击关闭富途增强。原系统其余行为完全不变。

set -e

ENV_FILE="$HOME/.market_news/futu_env"
if [ -f "$ENV_FILE" ]; then
  rm "$ENV_FILE"
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$SCRIPT_DIR/market_news_stack.command"

osascript -e 'display dialog "✅ 富途增强已关闭\n\n• notify 周期恢复原行为\n• 已存在的 reports/live/latest_phone_alert_enriched.json 不会被自动删除\n  （留作历史参考；不影响推送）" buttons {"知道了"} default button "知道了" with title "Futu Enrichment Disabled"' >/dev/null 2>&1 || true
