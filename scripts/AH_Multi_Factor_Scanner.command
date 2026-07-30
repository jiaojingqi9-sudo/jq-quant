#!/bin/zsh
# 双击运行 AH 多因子扫描器。
# 产物：
#   reports/live/scan_universe.json
#   reports/live/scan_limit_up_streak.json   ← A 股连板榜
#   reports/live/scan_volume_shrink_up.json  ← 缩量上涨榜
#   reports/live/scan_near_ath.json          ← 接近历史新高榜
#   reports/live/scan_summary.json           ← 元数据 / 错误清单
#
# 第一次跑会慢一点（要拉 3 年历史 K 线）；OpenD 没起的话会优雅退出，不报错。

set -e

PROJECT_DIR="/Users/jiao/All here/news collector"
cd "$PROJECT_DIR"

python_bin="$(command -v python3)"
if [ -z "$python_bin" ]; then
  osascript -e 'display alert "找不到 python3" message "Please install Python 3 or check your PATH." as critical'
  exit 1
fi

echo "▶ Running AH multi-factor scanner..."
echo "  markets: HK,SH,SZ  (override via MARKET_NEWS_AH_SCANNER_MARKETS=HK,US ...)"
echo "  output : $PROJECT_DIR/reports/live/scan_*.json"
echo

# Pass --update-universe so the dynamic universe JSON is also written.
# (It's only ACTIVATED if Enable_Dynamic_Universe.command was clicked.)
"$python_bin" -m market_news ah-scan --update-universe

echo
echo "✅ Done. To see the four boards:"
echo "   open '$PROJECT_DIR/reports/live'"
echo
echo "Closing in 5s..."
sleep 5
