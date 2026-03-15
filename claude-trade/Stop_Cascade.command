#!/bin/bash
# ══════════════════════════════════════════════════════════════════════
# Stop_Cascade.command — 停止级联策略引擎
# Gracefully stops the running Cascade engine.
# ══════════════════════════════════════════════════════════════════════

cd "$(dirname "$0")"

PID_FILE="runtime/engine.pid"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   Stopping Cascade Engine — 停止引擎     ║"
echo "╚══════════════════════════════════════════╝"
echo ""

if [ ! -f "$PID_FILE" ]; then
    echo "  No PID file found — engine may not be running."
    echo "  (Expected: $PID_FILE)"
else
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "  Sending graceful shutdown to PID $PID …"
        kill -TERM "$PID"
        # Wait up to 10 seconds
        for i in $(seq 1 10); do
            sleep 1
            if ! kill -0 "$PID" 2>/dev/null; then
                echo "  ✓ Engine stopped (PID $PID)."
                rm -f "$PID_FILE"
                break
            fi
            echo "  Waiting … ($i/10)"
        done
        if kill -0 "$PID" 2>/dev/null; then
            echo "  Engine did not stop gracefully — forcing …"
            kill -KILL "$PID"
            rm -f "$PID_FILE"
            echo "  ✓ Engine killed."
        fi
    else
        echo "  Process $PID is not running — removing stale PID file."
        rm -f "$PID_FILE"
    fi
fi

echo ""
echo "  Done. Press Enter to close."
read -r
