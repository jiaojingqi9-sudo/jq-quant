#!/bin/zsh
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_DIR"

# Prefer the project venv: pinned deps + working certifi. The anaconda
# python3.13 previously used here caused SSL "certificate verify locations"
# failures during yfinance downloads (see runtime/futu_stock_screener_app.log).
if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
  "$PROJECT_DIR/.venv/bin/python" stock/tools/futu_stock_screener_desktop.py
elif command -v uv >/dev/null 2>&1; then
  uv run python stock/tools/futu_stock_screener_desktop.py
elif [ -x "$HOME/.local/bin/uv" ]; then
  "$HOME/.local/bin/uv" run python stock/tools/futu_stock_screener_desktop.py
else
  /opt/anaconda3/bin/python3 stock/tools/futu_stock_screener_desktop.py
fi
