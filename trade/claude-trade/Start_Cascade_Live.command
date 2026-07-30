#!/bin/bash
# ══════════════════════════════════════════════════════════════════════
# Start_Cascade_Live.command — 启动级联策略（真实交易）
# Starts the Cascade trading engine in LIVE trading mode.
# ⚠  WARNING: This will submit REAL orders.
# ══════════════════════════════════════════════════════════════════════

cd "$(dirname "$0")"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   Claude-Trade — Cascade Strategy 级联       ║"
echo "║   ⚠  Mode: LIVE TRADING — 真实交易模式 ⚠    ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "  This script will submit REAL ORDERS to your broker."
echo "  Make sure your .env has the correct account settings."
echo ""
echo "  Required .env settings for live trading:"
echo "    FUTU_ENABLE_REAL_TRADING=true"
echo "    FUTU_ALLOW_AUTO_REAL=true"
echo "    FUTU_TRD_ENV=REAL"
echo ""

# Safety confirmation
echo -n "  Type 'LIVE' to confirm real trading, or anything else to abort: "
read -r CONFIRM

if [ "$CONFIRM" != "LIVE" ]; then
    echo ""
    echo "  Aborted. No orders were submitted."
    echo "  Use Start_Cascade.command for paper trading (模拟盘)."
    sleep 3
    exit 0
fi

# ── Activate virtual environment ──────────────────────────────────────────
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
elif [ -f "../.venv/bin/activate" ]; then
    source ../.venv/bin/activate
fi

if ! command -v claude-trade &>/dev/null; then
    pip install -e . --quiet
fi

echo ""
echo "  Starting LIVE engine …  Press Ctrl-C to stop."
echo ""

claude-trade run

echo ""
echo "Engine stopped. Press Enter to close."
read -r
