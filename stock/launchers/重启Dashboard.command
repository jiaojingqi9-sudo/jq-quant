#!/bin/zsh
# 直接重启 streamlit dashboard（不依赖控制台重启按钮）

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_DIR"

# 先杀掉已有的 streamlit
pkill -f "streamlit.*8501" 2>/dev/null
sleep 1

if [[ ! -x ".venv/bin/python" ]]; then
  osascript -e 'display alert "缺少 .venv/bin/python" message "Python 环境丢失，请联系 Claude 重建依赖。" as critical'
  exit 1
fi

export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

echo "正在启动 Dashboard..."
nohup ".venv/bin/streamlit" run "src/taa_futu/dashboard_app.py" \
  --server.port 8501 \
  --server.headless true \
  >> /tmp/taa_dashboard.log 2>&1 &

sleep 2

if pgrep -f "streamlit.*8501" > /dev/null 2>&1; then
  echo "Dashboard 启动成功！"
  open "http://localhost:8501"
  sleep 1
  osascript -e 'tell application "Terminal" to close front window'
else
  echo "启动失败，查看日志："
  cat /tmp/taa_dashboard.log | tail -20
  echo ""
  echo "按任意键关闭..."
  read -k1
fi
