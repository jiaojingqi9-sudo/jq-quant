#!/bin/zsh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_DIR"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "未找到 .venv/bin/python，请先完成项目安装。"
  read -r "?按回车退出 / Press Enter to exit..."
  exit 1
fi

echo "正在启动 Crypto OFIM Binance 独立 App..."
echo "浏览器地址：http://localhost:8503"
".venv/bin/python" -m taa_futu.cli crypto-ofim-app --port 8503
