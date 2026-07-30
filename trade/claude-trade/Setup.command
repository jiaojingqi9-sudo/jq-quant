#!/bin/bash
# ══════════════════════════════════════════════════════════════════════
# Setup.command — 一键安装配置 / One-click setup
#
# 功能 / Does:
#   1. 检查 Python 3.11+
#   2. 创建虚拟环境 .venv
#   3. 安装全部依赖 (futu-api, ccxt, dash, plotly, ...)
#   4. 测试富途 OpenD 连接
#   5. 在桌面创建「总控制台」快捷方式
# ══════════════════════════════════════════════════════════════════════

cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"
DESKTOP="$HOME/Desktop"

# ── 颜色 / Colors ─────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

print_header() {
    echo ""
    echo -e "${BOLD}╔══════════════════════════════════════════════╗${RESET}"
    echo -e "${BOLD}║   Claude-Trade  Setup  安装配置              ║${RESET}"
    echo -e "${BOLD}╚══════════════════════════════════════════════╝${RESET}"
    echo ""
}

ok()   { echo -e "  ${GREEN}✓${RESET}  $1"; }
fail() { echo -e "  ${RED}✗${RESET}  $1"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $1"; }
info() { echo -e "  ${CYAN}▶${RESET}  $1"; }

print_header

# ══════════════════════════════════════════════════════════════════════
# Step 1: Python 版本检查
# ══════════════════════════════════════════════════════════════════════
info "Step 1/5  检查 Python 版本 …"

PYTHON=""
for cmd in python3.13 python3.12 python3.11 python3; do
    if command -v "$cmd" &>/dev/null; then
        VER=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        MAJOR=$(echo "$VER" | cut -d. -f1)
        MINOR=$(echo "$VER" | cut -d. -f2)
        if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 11 ]; then
            PYTHON="$cmd"
            ok "Python $VER ($cmd)"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    fail "需要 Python 3.11+，当前未找到"
    echo ""
    echo -e "  ${YELLOW}请安装 Python 3.11+:${RESET}"
    echo "    brew install python@3.12"
    echo "  或从 https://python.org 下载安装包"
    echo ""
    read -r -p "  按 Enter 退出 …"
    exit 1
fi

# ══════════════════════════════════════════════════════════════════════
# Step 2: 创建虚拟环境
# ══════════════════════════════════════════════════════════════════════
echo ""
info "Step 2/5  创建虚拟环境 .venv …"

if [ -d ".venv" ] && [ -f ".venv/bin/activate" ]; then
    warn ".venv 已存在，跳过创建"
else
    "$PYTHON" -m venv .venv
    ok ".venv 创建成功"
fi

source .venv/bin/activate
ok "虚拟环境已激活 ($("$PYTHON" --version))"

# ══════════════════════════════════════════════════════════════════════
# Step 3: 安装依赖
# ══════════════════════════════════════════════════════════════════════
echo ""
info "Step 3/5  安装依赖包 (首次约需 2-5 分钟) …"
echo ""

# Upgrade pip silently
pip install --upgrade pip --quiet

# Install the package and all deps
pip install -e ".[dev]" --quiet 2>&1 | grep -v "^$" | grep -v "already satisfied" | \
  grep -v "Downloading" | grep -v "Installing" | head -10 || true

# Verify key packages
MISSING=()
for pkg in futu pandas numpy ccxt dash plotly dotenv; do
    python3 -c "import $pkg" 2>/dev/null && ok "$pkg" || { fail "$pkg (未安装)"; MISSING+=("$pkg"); }
done

if [ ${#MISSING[@]} -gt 0 ]; then
    warn "部分包安装失败，尝试单独安装 …"
    pip install futu-api ccxt dash plotly pandas numpy python-dotenv --quiet
fi

# ══════════════════════════════════════════════════════════════════════
# Step 4: 测试富途 OpenD 连接
# ══════════════════════════════════════════════════════════════════════
echo ""
info "Step 4/5  检测富途 OpenD 连接 …"

FUTU_HOST=$(grep "^FUTU_HOST=" .env 2>/dev/null | cut -d= -f2 | tr -d ' ')
FUTU_PORT=$(grep "^FUTU_PORT=" .env 2>/dev/null | cut -d= -f2 | tr -d ' ')
FUTU_HOST="${FUTU_HOST:-127.0.0.1}"
FUTU_PORT="${FUTU_PORT:-11111}"

python3 - <<PYEOF
import socket, sys
try:
    with socket.create_connection(("$FUTU_HOST", int("$FUTU_PORT")), timeout=2):
        print("  \033[0;32m✓\033[0m  OpenD 在 $FUTU_HOST:$FUTU_PORT 正常运行")
except Exception:
    print("  \033[1;33m⚠\033[0m  OpenD 未检测到 ($FUTU_HOST:$FUTU_PORT)")
    print("     → 请打开「富途牛牛」→ 设置 → OpenAPI → 开启")
    print("     → 然后重新运行 Check_OpenD.command 验证")
PYEOF

# ══════════════════════════════════════════════════════════════════════
# Step 5: 创建桌面总控制台
# ══════════════════════════════════════════════════════════════════════
echo ""
info "Step 5/5  在桌面创建总控制台快捷方式 …"

CONSOLE_SRC="$PROJECT_DIR/总控制台.command"
CONSOLE_DST="$DESKTOP/Claude-Trade 总控制台.command"

if [ -f "$CONSOLE_SRC" ]; then
    cp "$CONSOLE_SRC" "$CONSOLE_DST"
    chmod +x "$CONSOLE_DST"
    ok "桌面快捷方式已创建：$(basename "$CONSOLE_DST")"
else
    warn "找不到 总控制台.command，跳过桌面快捷方式"
fi

# ══════════════════════════════════════════════════════════════════════
# 完成
# ══════════════════════════════════════════════════════════════════════
echo ""
echo -e "${BOLD}══════════════════════════════════════════════${RESET}"
echo -e "${GREEN}${BOLD}  ✓  安装完成！Setup Complete!${RESET}"
echo -e "${BOLD}══════════════════════════════════════════════${RESET}"
echo ""
echo "  下一步 / Next steps:"
echo "    1. 双击桌面「Claude-Trade 总控制台」启动"
echo "    2. 编辑 .env 填入你的配置 (已预填合理默认值)"
echo "    3. 如使用真实账户，参考 .env 中的注释"
echo ""
echo "  项目路径 / Project path:"
echo "    $PROJECT_DIR"
echo ""
read -r -p "  按 Enter 关闭 …"
