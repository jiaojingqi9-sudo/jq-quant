#!/bin/bash
# ══════════════════════════════════════════════════════════════════════
# Show_Status.command — 查看引擎状态
# Displays the current engine status, regime, and portfolio summary.
# ══════════════════════════════════════════════════════════════════════

cd "$(dirname "$0")"

echo ""

# ── Activate virtual environment ──────────────────────────────────────────
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
elif [ -f "../.venv/bin/activate" ]; then
    source ../.venv/bin/activate
fi

if ! command -v claude-trade &>/dev/null; then
    pip install -e . --quiet
fi

claude-trade status

echo ""
echo "Press Enter to close."
read -r
