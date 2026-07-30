#!/bin/zsh
# 双击启用：让 AH 催化板块用 ah-scan 生成的动态 universe（而不是静态的 tech_universe_cn_hk.json）。
#
# 前提：先双击过 AH_Multi_Factor_Scanner.command 至少一次，
# 已经生成 config/tech_universe_cn_hk.dynamic.json。
# 没生成的话这里也会自动帮你跑一次。

set -e

PROJECT_DIR="/Users/jiao/All here/news collector"
DYN_FILE="$PROJECT_DIR/config/tech_universe_cn_hk.dynamic.json"

if [ ! -f "$DYN_FILE" ]; then
  echo "Dynamic universe file not found. Running scanner first..."
  "$(cd "$(dirname "$0")" && pwd)/AH_Multi_Factor_Scanner.command"
fi

if [ ! -f "$DYN_FILE" ]; then
  osascript -e 'display alert "未能生成动态 universe" message "扫描器未能产出 tech_universe_cn_hk.dynamic.json。检查 OpenD 是否在线，看终端输出。" as critical'
  exit 1
fi

mkdir -p "$HOME/.market_news"
ENV_FILE="$HOME/.market_news/futu_env"
# Append (or upsert) the dynamic universe flag without disturbing other vars.
touch "$ENV_FILE"
grep -v "^export MARKET_NEWS_TECH_UNIVERSE_DYNAMIC=" "$ENV_FILE" > "$ENV_FILE.tmp" || true
mv "$ENV_FILE.tmp" "$ENV_FILE"
echo "export MARKET_NEWS_TECH_UNIVERSE_DYNAMIC=1" >> "$ENV_FILE"
chmod 600 "$ENV_FILE"

# Refresh launchd plists so collect picks up the env var on next cycle.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$SCRIPT_DIR/market_news_stack.command" 2>/dev/null || true

osascript -e 'display dialog "✅ 动态 universe 已启用\n\n• AH 催化板块改用扫描器生成的成分股\n• 静态文件 tech_universe_cn_hk.json 保持原样作备份\n• 想换回静态：双击 Disable_Dynamic_Universe.command" buttons {"知道了"} default button "知道了" with title "Dynamic Universe Enabled"' >/dev/null 2>&1 || true
