#!/bin/zsh
# 开启 pre-gate 的"只记录不执行"模式。
#
# Fusion Intraday 每个 cycle 计算 pre-gate 决策并写入 runtime/stock_events.jsonl
# （event_type = fusion_pregate_decision），但不会真删除任何候选，下单结果保持原样。
#
# 推荐用一天，观察 pre-gate 会把什么删掉，决定阈值合不合理，再切到 Pregate_Active。

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
  osascript -e 'display alert "trade/.env 不存在" message "请先在项目根目录运行过控制台，让它生成 .env。" as critical'
  exit 1
fi

python3 - <<'PY'
from pathlib import Path

env_file = Path("$(cd "$(dirname "$0")/../.." && pwd)/.env")
text = env_file.read_text(encoding="utf-8")

settings = {
    "FUSION_FUTU_PREGATE_ENABLED": "true",
    "FUSION_FUTU_PREGATE_LOG_ONLY": "true",
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
print("Pregate -> LOG ONLY")
PY

osascript -e 'display dialog "✅ Pre-gate 已开启（只记录不执行）\n\n• 每个 cycle 写决策到 runtime/stock_events.jsonl\n  搜索 fusion_pregate_decision 查看\n• 下单候选不被真删除\n• 跑一天后，觉得 OK 双击 Pregate_Active 切到生效模式" buttons {"知道了"} default button "知道了" with title "Pre-gate Log Only"' >/dev/null 2>&1 || true
