#!/bin/zsh
# 关闭 Fusion 盘前过滤（默认状态）。Fusion Intraday 行为与未集成 pre-gate 时一致。

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
  osascript -e 'display alert "trade/.env 不存在" message "请先在项目根目录运行过控制台，让它生成 .env。" as critical'
  exit 1
fi

python3 - <<'PY'
import re
from pathlib import Path

env_file = Path("$(cd "$(dirname "$0")/../.." && pwd)/.env")
text = env_file.read_text(encoding="utf-8")

settings = {
    "FUSION_FUTU_PREGATE_ENABLED": "false",
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
print("Pregate -> OFF")
PY

osascript -e 'display dialog "✅ Pre-gate 已关闭\n\nFusion Intraday 行为恢复成未集成 pre-gate 时一致。\n\n⚠️ 如果自动运行正在跑，下次 cycle 自动生效（不需要重启）。" buttons {"知道了"} default button "知道了" with title "Pre-gate Off"' >/dev/null 2>&1 || true
