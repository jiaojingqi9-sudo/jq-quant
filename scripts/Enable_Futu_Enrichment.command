#!/bin/zsh
# 双击启用：让每 5 分钟的 notify 自动调用富途接口给告警做数据增强。
# 增强结果写到 reports/live/latest_phone_alert_enriched.json，
# 原本的 WhatsApp 推送消息保持不变。
#
# 关闭：双击 Disable_Futu_Enrichment.command

set -e

mkdir -p "$HOME/.market_news"
ENV_FILE="$HOME/.market_news/futu_env"

# Idempotent: overwrite the toggle file with the flag set ON.
cat > "$ENV_FILE" <<'EOF'
# Auto-managed by Enable_Futu_Enrichment.command
# To disable, double-click Disable_Futu_Enrichment.command
export MARKET_NEWS_FUTU_ENRICHMENT=1
EOF
chmod 600 "$ENV_FILE"

# Reinstall the launchd plists so the new env-source line takes effect.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$SCRIPT_DIR/market_news_stack.command"

osascript -e 'display dialog "✅ 富途增强已开启\n\n• 每 5 分钟一次的 notify 周期会自动调用富途接口\n• 结果写到 reports/live/latest_phone_alert_enriched.json\n• WhatsApp 推送内容不变\n\n要关闭直接双击 Disable_Futu_Enrichment.command。" buttons {"知道了"} default button "知道了" with title "Futu Enrichment Enabled"' >/dev/null 2>&1 || true
