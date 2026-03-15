#!/bin/bash
# ══════════════════════════════════════════════════════════════════════
# Open_Dashboard.command — 打开 Web 控制面板
# Launches the Dash dashboard and opens it in your default browser.
# ══════════════════════════════════════════════════════════════════════

cd "$(dirname "$0")"

PORT=8051

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   Claude-Trade Dashboard — 控制面板      ║"
echo "║   URL: http://127.0.0.1:$PORT            ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Activate virtual environment ──────────────────────────────────────────
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
elif [ -f "../.venv/bin/activate" ]; then
    source ../.venv/bin/activate
fi

if ! command -v claude-trade &>/dev/null; then
    echo "▶ Installing claude-trade …"
    pip install -e . --quiet
fi

# Check if Dash is installed
python -c "import dash" 2>/dev/null || {
    echo "▶ Installing dashboard dependencies …"
    pip install dash plotly --quiet
}

# Open browser after short delay
(sleep 2 && open "http://127.0.0.1:$PORT") &

echo "  Starting dashboard on port $PORT …"
echo "  Your browser will open automatically."
echo "  Press Ctrl-C to stop the dashboard."
echo ""

claude-trade dashboard --port "$PORT"

echo ""
echo "Dashboard stopped. Press Enter to close."
read -r
