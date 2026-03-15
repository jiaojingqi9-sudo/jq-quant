#!/bin/bash
# ══════════════════════════════════════════════════════════════════════
# 总控制台.command — Claude-Trade 主控制台
# Master control console for Claude-Trade / Cascade Strategy
#
# 双击运行 / Double-click to launch from Finder or Desktop
# ══════════════════════════════════════════════════════════════════════

# ── 定位项目目录 / Locate project ────────────────────────────────────
# This file may be in the project root OR on the Desktop (as a copy).
# We detect the project from the script's real location or a stored path.

SCRIPT_PATH="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

# If running from Desktop, look for the project via a stored path file
DESKTOP_MARKER="$HOME/.claude_trade_project_path"
if [ -f "$DESKTOP_MARKER" ]; then
    PROJECT_DIR=$(cat "$DESKTOP_MARKER")
else
    PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
fi

# Save project path for future Desktop runs
echo "$PROJECT_DIR" > "$DESKTOP_MARKER"

# ── Colors ───────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BLUE='\033[0;34m'; BOLD='\033[1m'; DIM='\033[2m'
MAGENTA='\033[0;35m'; RESET='\033[0m'

# ── Status reading ────────────────────────────────────────────────────
_read_status() {
    STATUS_FILE="$PROJECT_DIR/runtime/status.json"
    if [ -f "$STATUS_FILE" ]; then
        python3 - "$STATUS_FILE" <<'PYEOF'
import json, sys, os
from datetime import datetime
from zoneinfo import ZoneInfo

f = sys.argv[1]
try:
    s = json.loads(open(f).read())
    regime  = s.get("regime","?")
    score   = s.get("regime_score",0)
    mode    = s.get("mode","?")
    exp     = float(s.get("total_exposure",0))*100
    acct    = float(s.get("account_value",0))
    cycles  = s.get("cycle_count",0)
    errors  = s.get("error_count",0)
    upd     = s.get("updated_at","")
    weights = s.get("target_weights",{})
    rd      = s.get("regime_details",{})
    budgets = s.get("asset_class_budgets",{})
    mkt_open = s.get("market_hours_open")

    try:
        ts = datetime.fromisoformat(upd).astimezone(ZoneInfo("America/New_York"))
        upd = ts.strftime("%m/%d %H:%M ET")
    except Exception:
        pass

    # Read initial capital from .env next to status file
    init_cap = 0.0
    env_file = os.path.join(os.path.dirname(f), "..", ".env")
    try:
        for line in open(env_file):
            if line.startswith("INITIAL_CAPITAL="):
                init_cap = float(line.split("=",1)[1].strip())
                break
    except Exception:
        pass

    # P&L from account_history.jsonl
    prev_acct = acct
    history_file = os.path.join(os.path.dirname(f), "account_history.jsonl")
    try:
        lines = open(history_file).readlines()
        if len(lines) >= 2:
            prev_acct = json.loads(lines[-2]).get("account_value", acct)
    except Exception:
        pass

    print(f"REGIME={regime}")
    print(f"SCORE={score:+.3f}")
    print(f"MODE={mode}")
    print(f"EXPOSURE={exp:.1f}")
    print(f"ACCT={acct:.2f}")
    print(f"INIT_CAP={init_cap:.2f}")
    print(f"PREV_ACCT={prev_acct:.2f}")
    print(f"CYCLES={cycles}")
    print(f"ERRORS={errors}")
    print(f"UPDATED={upd}")
    print(f"FUTU_ONLINE={'YES' if s.get('futu_online') else 'NO'}")
    print(f"CRYPTO_ONLINE={'YES' if s.get('crypto_online') else 'NO'}")
    print(f"MKT_OPEN={'YES' if mkt_open else 'NO' if mkt_open is not None else 'UNK'}")

    # Regime details
    det = rd.get("details", {})
    print(f"CRYPTO_PULSE={rd.get('crypto_pulse',0.0):+.3f}")
    print(f"VOL_REGIME={rd.get('vol_regime','?')}")
    print(f"CROSS_ASSET={rd.get('cross_asset_flow',0.0):+.3f}")
    print(f"FUNDING_SIG={rd.get('funding_signal',0.0):+.3f}")
    vix = det.get("vix_level")
    print(f"VIX={vix:.1f}" if vix is not None else "VIX=")
    fr = det.get("funding_rate")
    print(f"FUNDING_RATE={fr:.4%}" if fr is not None else "FUNDING_RATE=")
    bw = det.get("btc_weekend_return")
    print(f"BTC_WEEKEND={bw:+.2%}" if bw is not None else "BTC_WEEKEND=")

    # Asset class budgets
    for cls in ["equity","crypto","bond"]:
        v = budgets.get(cls, 0.0)
        if v > 0.001:
            print(f"BUDGET_{cls.upper()}={v*100:.1f}")

    # Target weights (all, not just top 4)
    for sym, w in sorted(weights.items(), key=lambda x: -x[1]):
        print(f"WEIGHT={sym}:{w*100:.1f}%")
except Exception as e:
    print(f"REGIME=UNKNOWN")
PYEOF
    else
        echo "REGIME=NOT_STARTED"
    fi
}

# ── Engine running check ──────────────────────────────────────────────
_engine_running() {
    PID_FILE="$PROJECT_DIR/runtime/engine.pid"
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        kill -0 "$PID" 2>/dev/null && echo "YES" || echo "NO"
    else
        echo "NO"
    fi
}

# ── Check OpenD ───────────────────────────────────────────────────────
_check_opend() {
    FUTU_HOST=$(grep "^FUTU_HOST=" "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d ' ')
    FUTU_PORT=$(grep "^FUTU_PORT=" "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d ' ')
    FUTU_HOST="${FUTU_HOST:-127.0.0.1}"
    FUTU_PORT="${FUTU_PORT:-11111}"
    python3 -c "
import socket
try:
    with socket.create_connection(('$FUTU_HOST', $FUTU_PORT), timeout=1):
        print('YES')
except Exception:
    print('NO')
" 2>/dev/null
}

# ── Check Binance / crypto API reachability ───────────────────────────
_check_binance() {
    # Read exchange name from .env; default to binance
    CRYPTO_EX=$(grep "^CRYPTO_EXCHANGE=" "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d ' ')
    CRYPTO_EX="${CRYPTO_EX:-binance}"
    python3 -c "
import urllib.request, json
ex = '$CRYPTO_EX'.lower()
# Binance public ping; fall back to generic HTTPS test for other exchanges
urls = {
    'binance':  'https://api.binance.com/api/v3/ping',
    'okx':      'https://www.okx.com/api/v5/public/time',
    'bybit':    'https://api.bybit.com/v5/market/time',
}
url = urls.get(ex, urls['binance'])
try:
    urllib.request.urlopen(url, timeout=3)
    print('YES')
except Exception:
    print('NO')
" 2>/dev/null
}

# ══════════════════════════════════════════════════════════════════════
# Main display loop
# ══════════════════════════════════════════════════════════════════════

show_dashboard() {
    clear

    # ── Header ──────────────────────────────────────────────────────
    echo -e "${BOLD}${CYAN}"
    echo "  ╔══════════════════════════════════════════════════════════╗"
    echo "  ║          ⛓  Claude-Trade  总控制台                      ║"
    echo "  ║             Cascade Strategy  级联多市场策略              ║"
    echo "  ╚══════════════════════════════════════════════════════════╝"
    echo -e "${RESET}"

    # ── Connectivity checks ─────────────────────────────────────────
    RUNNING=$(_engine_running)
    OPEND=$(_check_opend)
    BINANCE=$(_check_binance)

    ENGINE_STATUS="${RED}○ 已停止${RESET}"
    [ "$RUNNING" = "YES" ] && ENGINE_STATUS="${GREEN}● 运行中${RESET}"

    OPEND_STATUS="${RED}✗ OpenD 未连接${RESET}"
    [ "$OPEND" = "YES" ] && OPEND_STATUS="${GREEN}✓ OpenD 已连接${RESET}"

    # Detect exchange name for display
    CRYPTO_EX=$(grep "^CRYPTO_EXCHANGE=" "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d ' ')
    CRYPTO_EX="${CRYPTO_EX:-Binance}"
    CRYPTO_EX_UPPER=$(echo "$CRYPTO_EX" | tr '[:lower:]' '[:upper:]')

    BINANCE_STATUS="${RED}✗ ${CRYPTO_EX_UPPER} 不可达${RESET}"
    [ "$BINANCE" = "YES" ] && BINANCE_STATUS="${GREEN}✓ ${CRYPTO_EX_UPPER} 可达${RESET}"

    echo -e "  引擎状态:  $ENGINE_STATUS"
    echo -e "  富途:      $OPEND_STATUS     加密:  $BINANCE_STATUS"

    # ── Warn if crypto is unreachable ────────────────────────────────
    if [ "$BINANCE" = "NO" ]; then
        echo -e "  ${YELLOW}${BOLD}⚠  加密行情不可用 — Cascade 将以纯股票模式运行（regime 检测退化）${RESET}"
    fi
    echo ""

    # ── Portfolio status ─────────────────────────────────────────────
    SDATA=$(_read_status)

    if echo "$SDATA" | grep -q "^REGIME=NOT_STARTED"; then
        echo -e "  ${DIM}尚未运行 — 使用下方菜单启动引擎${RESET}"
    elif echo "$SDATA" | grep -q "^REGIME=UNKNOWN"; then
        echo -e "  ${YELLOW}状态文件损坏，请重启引擎${RESET}"
    else
        REGIME=$(echo "$SDATA"        | grep "^REGIME="        | cut -d= -f2)
        SCORE=$(echo "$SDATA"         | grep "^SCORE="         | cut -d= -f2)
        MODE=$(echo "$SDATA"          | grep "^MODE="          | cut -d= -f2)
        EXP=$(echo "$SDATA"           | grep "^EXPOSURE="      | cut -d= -f2)
        ACCT=$(echo "$SDATA"          | grep "^ACCT="          | cut -d= -f2)
        INIT_CAP=$(echo "$SDATA"      | grep "^INIT_CAP="      | cut -d= -f2)
        PREV_ACCT=$(echo "$SDATA"     | grep "^PREV_ACCT="     | cut -d= -f2)
        CYCLES=$(echo "$SDATA"        | grep "^CYCLES="        | cut -d= -f2)
        ERRORS=$(echo "$SDATA"        | grep "^ERRORS="        | cut -d= -f2)
        UPDATED=$(echo "$SDATA"       | grep "^UPDATED="       | cut -d= -f2)
        FUTU_ONLINE=$(echo "$SDATA"   | grep "^FUTU_ONLINE="   | cut -d= -f2)
        CRYPTO_ONLINE=$(echo "$SDATA" | grep "^CRYPTO_ONLINE=" | cut -d= -f2)
        MKT_OPEN=$(echo "$SDATA"      | grep "^MKT_OPEN="      | cut -d= -f2)
        CRYPTO_PULSE=$(echo "$SDATA"  | grep "^CRYPTO_PULSE="  | cut -d= -f2)
        VOL_REGIME=$(echo "$SDATA"    | grep "^VOL_REGIME="    | cut -d= -f2)
        CROSS_ASSET=$(echo "$SDATA"   | grep "^CROSS_ASSET="   | cut -d= -f2)
        FUNDING_SIG=$(echo "$SDATA"   | grep "^FUNDING_SIG="   | cut -d= -f2)
        VIX=$(echo "$SDATA"           | grep "^VIX="           | cut -d= -f2)
        FUNDING_RATE=$(echo "$SDATA"  | grep "^FUNDING_RATE="  | cut -d= -f2)
        BTC_WEEKEND=$(echo "$SDATA"   | grep "^BTC_WEEKEND="   | cut -d= -f2)
        BUD_EQUITY=$(echo "$SDATA"    | grep "^BUDGET_EQUITY=" | cut -d= -f2)
        BUD_CRYPTO=$(echo "$SDATA"    | grep "^BUDGET_CRYPTO=" | cut -d= -f2)
        BUD_BOND=$(echo "$SDATA"      | grep "^BUDGET_BOND="   | cut -d= -f2)

        # Regime colour
        case "$REGIME" in
            CRISIS)   RC="${RED}${BOLD}"   ;;
            CAUTIOUS) RC="${YELLOW}"       ;;
            NEUTRAL)  RC="${CYAN}"         ;;
            BULLISH)  RC="${GREEN}${BOLD}" ;;
            EUPHORIA) RC="${MAGENTA}${BOLD}";;
            *)        RC="${RESET}"        ;;
        esac

        MODE_STR="${YELLOW}模拟盘${RESET}"
        [ "$MODE" = "live" ] && MODE_STR="${GREEN}${BOLD}实盘交易${RESET}"

        ERR_STR="${DIM}$ERRORS${RESET}"
        [ "$ERRORS" -gt 0 ] 2>/dev/null && ERR_STR="${RED}$ERRORS${RESET}"

        # Exchange online indicators
        F_STR="${DIM}?${RESET}"
        [ "$FUTU_ONLINE" = "YES" ]   && F_STR="${GREEN}✓ 富途在线${RESET}"
        [ "$FUTU_ONLINE" = "NO" ]    && F_STR="${RED}✗ 富途离线${RESET}"
        C_STR="${DIM}?${RESET}"
        [ "$CRYPTO_ONLINE" = "YES" ] && C_STR="${GREEN}✓ 加密在线${RESET}"
        [ "$CRYPTO_ONLINE" = "NO" ]  && C_STR="${YELLOW}✗ 仅股票模式${RESET}"

        # Market hours
        MKT_STR="${DIM}?${RESET}"
        [ "$MKT_OPEN" = "YES" ] && MKT_STR="${GREEN}开市${RESET}"
        [ "$MKT_OPEN" = "NO" ]  && MKT_STR="${YELLOW}休市${RESET}"

        echo -e "  市场状态:  ${RC}${REGIME}${RESET}  (分数 ${SCORE})    美股: $MKT_STR"
        echo -e "  交易所:    $F_STR   $C_STR"
        echo -e "  交易模式:  $MODE_STR      周期: ${CYCLES}  错误: $ERR_STR"
        echo -e "  更新时间:  ${DIM}${UPDATED}${RESET}"

        # Warn: equity-only degraded mode
        if [ "$CRYPTO_ONLINE" = "NO" ] && [ "$RUNNING" = "YES" ]; then
            echo -e "  ${YELLOW}${BOLD}⚠  BTC/ETH 信号缺失，Cascade 运行在纯股票模式${RESET}"
        fi
        echo ""

        # ── P&L ──────────────────────────────────────────────────────
        echo -e "  ${DIM}── 账户盈亏 P&L ─────────────────────────────────${RESET}"
        echo -e "  账户净值:  ${BOLD}\$${ACCT}${RESET}"

        if [ -n "$INIT_CAP" ] && [ "$INIT_CAP" != "0.00" ]; then
            # Compute P&L via python (bash can't do float math)
            python3 -c "
acct=float('${ACCT}'); init=float('${INIT_CAP}'); prev=float('${PREV_ACCT:-$ACCT}')
pnl=acct-init; pct=pnl/init*100 if init else 0
chg=acct-prev; chg_pct=chg/prev*100 if prev else 0
sign='+' if pnl>=0 else ''
csign='+' if chg>=0 else ''
col='\033[32m' if pnl>=0 else '\033[31m'
ccol='\033[32m' if chg>=0 else '\033[31m'
rst='\033[0m'
print(f'  初始资金:  \${init:,.2f}')
print(f'  总盈亏:    {col}{sign}\${pnl:,.2f}  ({sign}{pct:.2f}%){rst}')
print(f'  本次变动:  {ccol}{csign}\${chg:,.2f}  ({csign}{chg_pct:.2f}%){rst}')
"
        fi
        echo -e "  总仓位:    ${EXP}%"
        echo ""

        # ── Asset class budgets ───────────────────────────────────────
        if [ -n "$BUD_EQUITY" ] || [ -n "$BUD_CRYPTO" ] || [ -n "$BUD_BOND" ]; then
            echo -e "  ${DIM}── 资金分配预算 Budget ───────────────────────────${RESET}"
            [ -n "$BUD_EQUITY" ] && printf "  %-8s %5s%%  %s\n" "股票" "$BUD_EQUITY" "$(python3 -c "print('█'*int(float('${BUD_EQUITY}')/5))")"
            [ -n "$BUD_CRYPTO" ] && printf "  %-8s %5s%%  \033[36m%s\033[0m\n" "加密" "$BUD_CRYPTO" "$(python3 -c "print('█'*int(float('${BUD_CRYPTO}')/5))")"
            [ -n "$BUD_BOND"   ] && printf "  %-8s %5s%%  \033[33m%s\033[0m\n" "债券" "$BUD_BOND"   "$(python3 -c "print('█'*int(float('${BUD_BOND}')/5))")"
            echo ""
        fi

        # ── Target weights ────────────────────────────────────────────
        WEIGHTS=$(echo "$SDATA" | grep "^WEIGHT=")
        if [ -n "$WEIGHTS" ]; then
            echo -e "  ${DIM}── 目标仓位 Target Weights ───────────────────────${RESET}"
            echo "$WEIGHTS" | while IFS= read -r line; do
                ENTRY=$(echo "$line" | cut -d= -f2)
                SYM=$(echo "$ENTRY" | cut -d: -f1)
                PCT=$(echo "$ENTRY" | cut -d: -f2)
                # Cyan for crypto
                if echo "$SYM" | grep -q "/"; then
                    printf "  \033[36m%-18s %s\033[0m\n" "$SYM" "$PCT"
                else
                    printf "  %-18s %s\n" "$SYM" "$PCT"
                fi
            done
            echo ""
        fi

        # ── Crypto signals ────────────────────────────────────────────
        if [ "$CRYPTO_ONLINE" = "YES" ] || [ -n "$CRYPTO_PULSE" ]; then
            echo -e "  ${DIM}── 加密市场信号 Crypto Signals ──────────────────${RESET}"
            [ -n "$CRYPTO_PULSE"  ] && echo -e "  加密脉冲:  ${CYAN}${CRYPTO_PULSE}${RESET}    跨资产流: ${CROSS_ASSET}"
            [ -n "$VOL_REGIME"    ] && echo -e "  波动制度:  ${VOL_REGIME}       资金信号: ${FUNDING_SIG}"
            [ -n "$VIX"           ] && echo -e "  VIX:       ${VIX}"
            [ -n "$FUNDING_RATE"  ] && echo -e "  资金费率:  ${FUNDING_RATE}"
            [ -n "$BTC_WEEKEND"   ] && echo -e "  BTC周末:   ${BTC_WEEKEND}"
            echo ""
        fi
    fi

    echo ""
    echo -e "  ${DIM}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"

    # ── Menu ─────────────────────────────────────────────────────────
    echo ""
    echo -e "  ${BOLD}操作菜单 / Actions${RESET}"
    echo ""
    echo -e "   ${CYAN}[1]${RESET}  🚀 启动引擎 (模拟盘)     Start Engine (Paper)"
    echo -e "   ${CYAN}[2]${RESET}  💰 启动引擎 (真实交易)   Start Engine (LIVE)"
    echo -e "   ${CYAN}[3]${RESET}  🛑 停止引擎              Stop Engine"
    echo ""
    echo -e "   ${CYAN}[4]${RESET}  📊 打开 Web 控制面板     Open Dashboard"
    echo -e "   ${CYAN}[5]${RESET}  📋 查看状态              Show Status"
    echo -e "   ${CYAN}[6]${RESET}  💼 查看持仓              Show Positions"
    echo -e "   ${CYAN}[7]${RESET}  🌐 检测连接 (OpenD+加密)  Check Connectivity"
    echo ""
    echo -e "   ${CYAN}[8]${RESET}  📈 运行回测              Run Backtest"
    echo -e "   ${CYAN}[9]${RESET}  🔭 查看市场状态          Show Regime"
    echo -e "   ${CYAN}[0]${RESET}  🗂  查看标的池            Show Universe"
    echo ""
    echo -e "   ${CYAN}[c]${RESET}  ⚙️  编辑配置文件          Edit Config (.env)"
    echo -e "   ${CYAN}[r]${RESET}  🔄 刷新                  Refresh"
    echo -e "   ${CYAN}[q]${RESET}  ❌ 退出                  Quit"
    echo ""
    echo -e "  ${DIM}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
}

# ── Activate venv helper ──────────────────────────────────────────────
_activate() {
    if [ -f "$PROJECT_DIR/.venv/bin/activate" ]; then
        source "$PROJECT_DIR/.venv/bin/activate"
    fi
}

# ── Execute in new Terminal tab ───────────────────────────────────────
_open_in_terminal() {
    local CMD="$1"
    osascript <<ASEOF
tell application "Terminal"
    do script "$CMD"
    activate
end tell
ASEOF
}

# ══════════════════════════════════════════════════════════════════════
# Main loop
# ══════════════════════════════════════════════════════════════════════

_activate

while true; do
    show_dashboard

    echo -n "  选择操作 / Choose action [1-9/c/r/q]: "
    read -r CHOICE

    case "$CHOICE" in

        1)  # Start engine (dry-run / paper)
            echo ""
            echo -e "  ${GREEN}▶ 在新窗口启动引擎 (模拟盘) …${RESET}"
            _open_in_terminal "cd '$PROJECT_DIR' && bash Start_Cascade.command"
            sleep 1
            ;;

        2)  # Start engine (LIVE)
            echo ""
            echo -e "  ${RED}${BOLD}⚠  真实交易模式 — 将开新窗口并要求输入确认${RESET}"
            _open_in_terminal "cd '$PROJECT_DIR' && bash Start_Cascade_Live.command"
            sleep 1
            ;;

        3)  # Stop engine
            echo ""
            echo -e "  ${YELLOW}▶ 停止引擎 …${RESET}"
            bash "$PROJECT_DIR/Stop_Cascade.command" 2>/dev/null || {
                PID_FILE="$PROJECT_DIR/runtime/engine.pid"
                if [ -f "$PID_FILE" ]; then
                    PID=$(cat "$PID_FILE")
                    kill -TERM "$PID" 2>/dev/null
                    rm -f "$PID_FILE"
                    echo -e "  ${GREEN}✓ 已停止${RESET}"
                else
                    echo -e "  ${YELLOW}⚠ 引擎未在运行${RESET}"
                fi
            }
            sleep 2
            ;;

        4)  # Open dashboard
            echo ""
            echo -e "  ${CYAN}▶ 启动 Web 控制面板 …${RESET}"
            _open_in_terminal "cd '$PROJECT_DIR' && bash Open_Dashboard.command"
            sleep 2
            ;;

        5)  # Status
            echo ""
            _activate
            claude-trade status 2>/dev/null || python3 -m claude_trade.cli status 2>/dev/null || echo "  运行 Setup.command 先安装依赖"
            echo ""
            read -r -p "  按 Enter 继续 …"
            ;;

        6)  # Positions
            echo ""
            _activate
            claude-trade positions 2>/dev/null || echo "  引擎未启动或无持仓"
            echo ""
            read -r -p "  按 Enter 继续 …"
            ;;

        7)  # Check connectivity (OpenD + crypto exchange)
            echo ""
            echo -e "  ${CYAN}▶ 检测连接 …${RESET}"
            echo ""
            # OpenD
            OPEND_NOW=$(_check_opend)
            if [ "$OPEND_NOW" = "YES" ]; then
                echo -e "  富途 OpenD:   ${GREEN}✓ 已连接${RESET}"
            else
                echo -e "  富途 OpenD:   ${RED}✗ 未连接  (请先启动 Futu OpenD App)${RESET}"
            fi
            # Crypto
            BINANCE_NOW=$(_check_binance)
            CNAME=$(grep "^CRYPTO_EXCHANGE=" "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d ' ')
            CNAME="${CNAME:-Binance}"
            if [ "$BINANCE_NOW" = "YES" ]; then
                echo -e "  ${CNAME}:      ${GREEN}✓ 公网可达（行情正常）${RESET}"
            else
                echo -e "  ${CNAME}:      ${RED}✗ 无法访问${RESET}"
                echo -e "  ${YELLOW}  → Cascade 将以纯股票模式运行，BTC/ETH regime 信号缺失${RESET}"
                echo -e "  ${YELLOW}  → 解决：开启 VPN，或在 .env 设置 CRYPTO_API_KEY${RESET}"
            fi
            echo ""
            read -r -p "  按 Enter 继续 …"
            ;;

        8)  # Backtest
            echo ""
            _activate
            claude-trade backtest 2>/dev/null || echo "  需要先安装依赖 (运行 Setup.command)"
            echo ""
            read -r -p "  按 Enter 继续 …"
            ;;

        9)  # Regime
            echo ""
            _activate
            claude-trade regime 2>/dev/null || echo "  引擎未运行，无缓存状态"
            echo ""
            read -r -p "  按 Enter 继续 …"
            ;;

        0)  # Show universe
            echo ""
            echo -e "  ${BOLD}标的池 / Universe${RESET}"
            echo ""
            python3 - "$PROJECT_DIR/.env" <<'PYEOF'
import os, sys
env_file = sys.argv[1] if len(sys.argv) > 1 else '.env'
cfg = {}
if os.path.exists(env_file):
    for line in open(env_file):
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, _, v = line.partition('=')
            cfg[k.strip()] = v.strip()

dm  = cfg.get('DM_UNIVERSE', 'US.SPY,US.EFA,US.AGG,US.GLD,BTC/USDT,ETH/USDT')
rsi = cfg.get('RSI_UNIVERSE', 'US.QQQ,SOL/USDT')
ex  = cfg.get('CRYPTO_EXCHANGE', 'Binance').upper()

dm_syms  = [s.strip() for s in dm.split(',') if s.strip()]
rsi_syms = [s.strip() for s in rsi.split(',') if s.strip()]

eq_dm  = [s for s in dm_syms  if s.startswith('US.') or s.startswith('HK.')]
cr_dm  = [s for s in dm_syms  if '/' in s]
eq_rsi = [s for s in rsi_syms if s.startswith('US.') or s.startswith('HK.')]
cr_rsi = [s for s in rsi_syms if '/' in s]

print(f"  DM Universe ({len(dm_syms)} symbols):")
if eq_dm:
    print(f"    股票/ETF (Futu):  {', '.join(eq_dm)}")
if cr_dm:
    print(f"    加密 ({ex}):     {', '.join(cr_dm)}")

print(f"\n  RSI Universe ({len(rsi_syms)} symbols):")
if eq_rsi:
    print(f"    股票/ETF (Futu):  {', '.join(eq_rsi)}")
if cr_rsi:
    print(f"    加密 ({ex}):     {', '.join(cr_rsi)}")

all_crypto = cr_dm + cr_rsi
if all_crypto:
    print(f"\n  ⚠  加密标的 ({len(all_crypto)} 个) 需要 {ex} 公网连接或 API Key")
    print(f"     连接失败时这些标的不参与策略: {', '.join(all_crypto)}")
PYEOF
            echo ""
            read -r -p "  按 Enter 继续 …"
            ;;

        c|C)  # Edit config
            echo ""
            echo -e "  ${CYAN}▶ 用 TextEdit 打开 .env …${RESET}"
            open -e "$PROJECT_DIR/.env"
            echo "  （编辑完成后保存，重启引擎生效）"
            sleep 1
            ;;

        r|R)  # Refresh
            ;;

        q|Q|"")
            echo ""
            echo -e "  ${DIM}再见 / Goodbye${RESET}"
            echo ""
            exit 0
            ;;

        *)
            echo -e "  ${YELLOW}无效输入，请重试${RESET}"
            sleep 1
            ;;
    esac
done
