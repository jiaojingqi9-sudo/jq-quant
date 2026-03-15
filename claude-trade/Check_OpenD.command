#!/bin/bash
# ══════════════════════════════════════════════════════════════════════
# Check_OpenD.command — 检测富途 OpenD 连接状态
# Tests connection to Futu OpenD and shows account info
# ══════════════════════════════════════════════════════════════════════

cd "$(dirname "$0")"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

# Load .env values
FUTU_HOST=$(grep "^FUTU_HOST=" .env 2>/dev/null | cut -d= -f2 | tr -d ' ')
FUTU_PORT=$(grep "^FUTU_PORT=" .env 2>/dev/null | cut -d= -f2 | tr -d ' ')
FUTU_TRD_ENV=$(grep "^FUTU_TRD_ENV=" .env 2>/dev/null | cut -d= -f2 | tr -d ' ')
FUTU_HOST="${FUTU_HOST:-127.0.0.1}"
FUTU_PORT="${FUTU_PORT:-11111}"
FUTU_TRD_ENV="${FUTU_TRD_ENV:-SIMULATE}"

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║   富途 OpenD 连接检测                    ║${RESET}"
echo -e "${BOLD}║   Futu OpenD Connection Check            ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  目标地址 / Target:  ${CYAN}${FUTU_HOST}:${FUTU_PORT}${RESET}"
echo -e "  交易环境 / Mode:    ${YELLOW}${FUTU_TRD_ENV}${RESET}"
echo ""

# ── Step 1: TCP Connectivity ──────────────────────────────────────────
echo "  [1/3] TCP 连接测试 …"
python3 - <<PYEOF
import socket
try:
    with socket.create_connection(("$FUTU_HOST", int("$FUTU_PORT")), timeout=3):
        print(f"  \033[0;32m✓\033[0m  TCP 连接成功 → $FUTU_HOST:$FUTU_PORT")
except ConnectionRefusedError:
    print(f"  \033[0;31m✗\033[0m  连接被拒绝 — OpenD 未启动或端口不对")
    print()
    print("       请检查:")
    print("         1. 打开「富途牛牛」应用")
    print("         2. 右上角 → 设置 → OpenAPI → 开启 OpenAPI")
    print("         3. 确认端口设置为 11111")
    print("         4. 防火墙未拦截端口 11111")
    exit(1)
except TimeoutError:
    print(f"  \033[1;33m⚠\033[0m  连接超时 — 检查主机地址: $FUTU_HOST")
    exit(1)
PYEOF
TCP_OK=$?

if [ $TCP_OK -ne 0 ]; then
    echo ""
    echo -e "  ${YELLOW}修复步骤 / Fix steps:${RESET}"
    echo "    1. 打开「富途牛牛」桌面客户端"
    echo "    2. 右上角齿轮 → 设置 → OpenAPI → 开启开关"
    echo "    3. 确认「监听端口」= 11111"
    echo "    4. 重启富途牛牛后再试"
    echo ""
    read -r -p "  按 Enter 关闭 …"
    exit 1
fi

# ── Step 2: futu-api 库检查 ────────────────────────────────────────────
echo ""
echo "  [2/3] futu-api Python 包检测 …"
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

python3 -c "import futu; print(f'  \033[0;32m✓\033[0m  futu-api {futu.__version__} 已安装')" 2>/dev/null || {
    echo -e "  ${YELLOW}⚠  futu-api 未安装 — 运行 Setup.command 先安装依赖${RESET}"
    echo ""
    read -r -p "  按 Enter 关闭 …"
    exit 1
}

# ── Step 3: API handshake + account list ──────────────────────────────
echo ""
echo "  [3/3] API 握手 & 账户列表 …"
python3 - <<PYEOF
import sys
sys.path.insert(0, 'src')

try:
    import futu
    from zoneinfo import ZoneInfo

    # Open quote context (just tests API access)
    qc = futu.OpenQuoteContext(host="$FUTU_HOST", port=int("$FUTU_PORT"))
    ret, data = qc.get_global_state()
    qc.close()

    if ret == futu.RET_OK:
        print(f"  \033[0;32m✓\033[0m  OpenD API 握手成功")
        market_state = data.get("market_state", {})
        server_ver   = data.get("server_ver", "?")
        print(f"       OpenD 版本: {server_ver}")
    else:
        print(f"  \033[1;33m⚠\033[0m  API 握手失败: {data}")

    # Open trade context and list accounts
    trd_market = "$( grep '^FUTU_TRD_MARKET=' .env | cut -d= -f2 | tr -d ' ' )"
    trd_market = trd_market or "US"
    trd_market_enum = getattr(futu.TrdMarket, trd_market, futu.TrdMarket.US)

    tc = futu.OpenSecTradeContext(
        filter_trdmarket=trd_market_enum,
        host="$FUTU_HOST", port=int("$FUTU_PORT")
    )
    ret, accounts = tc.get_acc_list()
    tc.close()

    if ret == futu.RET_OK and not accounts.empty:
        print(f"\n  \033[0;32m✓\033[0m  找到 {len(accounts)} 个账户:\n")
        print(f"  {'账户ID':<14} {'环境':<10} {'市场':<10} {'币种'}")
        print(f"  {'──────':<14} {'────':<10} {'────':<10} {'────'}")
        for _, row in accounts.iterrows():
            markets = row.get('trdmarket_auth', [])
            if isinstance(markets, list):
                markets_str = ','.join(markets[:3])
            else:
                markets_str = str(markets)
            acc_id  = row.get('acc_id', '?')
            trd_env = row.get('trd_env', '?')
            print(f"  {str(acc_id):<14} {str(trd_env):<10} {markets_str:<10}")
    else:
        print(f"  \033[1;33m⚠\033[0m  未找到 {trd_market} 市场账户: {accounts if ret != futu.RET_OK else '空'}")
        print(f"     → 确认已开通对应市场权限")

except Exception as e:
    print(f"  \033[0;31m✗\033[0m  错误: {e}")
    import traceback; traceback.print_exc()
PYEOF

echo ""
echo -e "${GREEN}${BOLD}  ✓  检测完成 / Check complete${RESET}"
echo ""
echo "  配置文件 / Config: .env"
echo "    FUTU_TRD_ENV=${FUTU_TRD_ENV}  (SIMULATE=模拟盘 / REAL=真实)"
echo ""
read -r -p "  按 Enter 关闭 …"
