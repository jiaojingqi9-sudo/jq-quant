#!/bin/bash
# ══════════════════════════════════════════════════════════════════════
# Start_Cascade.command — 启动级联策略（模拟盘）
# Starts the Cascade trading engine in DRY-RUN (paper trading) mode.
# Double-click this file in Finder to launch.
# ══════════════════════════════════════════════════════════════════════

# Change to the directory containing this script
cd "$(dirname "$0")"

SCRIPT_DIR="$(pwd)"
PROJECT_NAME="claude-trade"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   Claude-Trade — Cascade Strategy 级联   ║"
echo "║   Mode: DRY-RUN (paper trading 模拟盘)   ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Activate virtual environment (if present) ──────────────────────────────
if [ -f ".venv/bin/activate" ]; then
    echo "▶ Activating virtual environment …"
    source .venv/bin/activate
elif [ -f "../.venv/bin/activate" ]; then
    source ../.venv/bin/activate
fi

# ── Check that claude-trade is installed ──────────────────────────────────
if ! command -v claude-trade &>/dev/null; then
    echo "⚠  claude-trade not found — installing …"
    pip install -e . --quiet
fi

# ── Check .env exists ─────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        echo "⚠  .env not found — copying from .env.example"
        cp .env.example .env
        echo "   Please edit .env and re-run this script."
        read -r -p "   Press Enter to continue anyway …"
    fi
fi

echo ""
echo "Starting engine in DRY-RUN mode …"
echo "Press Ctrl-C to stop."
echo ""

# ── Launch ────────────────────────────────────────────────────────────────
claude-trade run --dry-run

echo ""
echo "Engine stopped. Press Enter to close this window."
read -r
