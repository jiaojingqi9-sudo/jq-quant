#!/bin/zsh
# 桌面快捷启动：直接打开 TAA + Futu 的可点击控制台

PROJECT_DIR="/Users/jiao/All here/trade"

if [[ ! -d "$PROJECT_DIR" ]]; then
  osascript -e 'display alert "找不到项目目录" message "请确认项目目录在 /Users/jiao/All here/trade" as critical'
  exit 1
fi

cd "$PROJECT_DIR"

if [[ ! -x ".venv/bin/python" ]]; then
  osascript -e 'display alert "缺少 Python 环境" message "找不到 .venv/bin/python，请先恢复项目环境。" as critical'
  exit 1
fi

export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
exec ".venv/bin/python" -m taa_futu.control_panel
