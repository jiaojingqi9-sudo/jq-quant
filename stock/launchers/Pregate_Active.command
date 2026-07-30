#!/bin/zsh
# Pre-gate 真正生效：Fusion 候选里被 pre-gate 标记为 filter 的标的不再下单。
#
# 建议先在 Pregate_LogOnly 模式跑过一天，确认决策合理再开这个。
# Pre-gate 只能减不能加；如果 OpenD 行情有问题，最差情况是少 trade 几只股票。

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
  osascript -e 'display alert "trade/.env 不存在" message "请先在项目根目录运行过控制台，让它生成 .env。" as critical'
  exit 1
fi

# Confirm with a dialog because this actually changes execution.
RESPONSE=$(osascript -e 'display dialog "⚠️ 切到 Pre-gate Active 模式？\n\nFusion 候选里被 pre-gate 标记为 filter 的标的将不会下单。\n\n建议先在 LogOnly 模式跑过一天确认阈值。" buttons {"取消","继续"} default button "继续" with title "Confirm Pre-gate Active"' 2>/dev/null || echo "取消")
if [[ ! "$RESPONSE" =~ "继续" ]]; then
  echo "Cancelled."
  exit 0
fi

python3 - <<'PY'
from pathlib import Path

env_file = Path("$(cd "$(dirname "$0")/../.." && pwd)/.env")
text = env_file.read_text(encoding="utf-8")

settings = {
    "FUSION_FUTU_PREGATE_ENABLED": "true",
    "FUSION_FUTU_PREGATE_LOG_ONLY": "false",
}

lines = text.splitlines()
present = {k: False for k in settings}
for i, line in enumerate(lines):
    for key, value in settings.items():
        if line.startswith(key + "="):
            lines[i] = f"{key}={value}"
            present[key] = True

for key, value in settings.items():
    if not present[key]:
        lines.append(f"{key}={value}")

env_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
print("Pregate -> ACTIVE")
PY

osascript -e 'display dialog "✅ Pre-gate 已激活\n\n• Fusion 下个 cycle 真按 pre-gate 决策过滤候选\n• 决策仍写入 runtime/stock_events.jsonl 备查\n• 想撤回：双击 Pregate_Off 或 Pregate_LogOnly" buttons {"知道了"} default button "知道了" with title "Pre-gate Active"' >/dev/null 2>&1 || true
