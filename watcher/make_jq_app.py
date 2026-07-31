#!/usr/bin/env python3
"""make_jq_app - 生成唯一入口 JQ Quant.app，并收拾桌面上的旧入口。

为什么：现在有 5 个 .app、16 个 .command、01_启动器 里 8 个，共 29 个入口，
用户自己都找不到该点哪个（她刚才就没找到）。

这个 app 做的事：
  1. 确保 FutuOpenD 在跑（不在就提示，不自作主张启动交易网关）
  2. 起 Streamlit 交易终端（已在跑就直接复用，不重复起）
  3. 用默认浏览器打开
所有子功能（加密、选股器、新闻、控制台）都在终端界面里，不再需要单独图标。

旧入口移到回收站而非删除，随时可取回。
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
ALL = HOME / "All here"
TRADE = ALL / "trade"
DESKTOP = HOME / "Desktop"
APP = DESKTOP / "JQ Quant.app"
TRASH = ALL / "_回收站_20260730" / "旧桌面入口"

# 桌面上要收走的旧入口
OLD_DESKTOP = [
    "TAA 量化交易.app",
    "Crypto OFIM Binance.app",
    "启动量化交易控制台.command",
    "市场新闻.command",
]

LAUNCHER = r'''#!/bin/zsh
# JQ Quant 唯一入口。
# 生成脚本：All here/futu_watcher/make_jq_app.py（改这里没用，改那边再重新生成）
set -uo pipefail

PROJECT="/Users/jiao/All here/trade"
PORT="${TAA_DASHBOARD_PORT:-8501}"
URL="http://localhost:${PORT}"
PY="${PROJECT}/.venv/bin/python"
APP_PY="${PROJECT}/src/taa_futu/dashboard_app.py"
RUNTIME="${PROJECT}/runtime"
LOG="${RUNTIME}/jq_quant.log"

mkdir -p "$RUNTIME"

note() { /usr/bin/osascript -e "display notification \"$1\" with title \"JQ Quant\"" >/dev/null 2>&1 || true; }
alert() { /usr/bin/osascript -e "display alert \"JQ Quant\" message \"$1\"" >/dev/null 2>&1 || true; }

# 用应用窗口模式打开，而不是普通浏览器标签页。
#
# Chrome/Edge 的 --app= 会开一个没有地址栏、没有标签栏、没有书签栏的独立窗口，
# 在 Dock 里也是独立条目——看起来就是一个应用，而不是"又打开一个网页"。
# 独立的 --user-data-dir 让它有自己的窗口尺寸与缩放记忆，不受你日常浏览影响。
open_window() {
  local url="$1"
  local profile="${RUNTIME}/chrome_app_profile"
  mkdir -p "$profile"
  for browser in "Google Chrome" "Microsoft Edge" "Brave Browser"; do
    if [[ -d "/Applications/${browser}.app" ]]; then
      /usr/bin/open -na "$browser" --args \
        --app="$url" \
        --user-data-dir="$profile" \
        --no-first-run \
        --no-default-browser-check
      return 0
    fi
  done
  # 没有 Chromium 系浏览器就退回默认浏览器，功能一样，只是外观像网页
  /usr/bin/open "$url"
}

if [ ! -x "$PY" ]; then
  alert "找不到 Python 环境：$PY

交易系统的依赖装在 trade/.venv 里。如果你搬动过文件夹或重装过系统，需要重新创建这个环境。"
  exit 1
fi

# 端口已通就直接开，不重复启动
if /usr/bin/nc -z 127.0.0.1 "$PORT" >/dev/null 2>&1; then
  open_window "$URL"
  exit 0
fi

# 富途网关没开时只提示，不代劳——交易网关该由用户自己决定何时连
if ! /usr/bin/nc -z 127.0.0.1 11111 >/dev/null 2>&1; then
  note "FutuOpenD 未运行，行情与账户会是空的"
fi

note "正在启动…"
nohup "$PY" -m streamlit run "$APP_PY" \
  --server.port "$PORT" \
  --server.headless true \
  --browser.gatherUsageStats false \
  >>"$LOG" 2>&1 &

# 等端口起来再开浏览器，避免打开一个报错页
for i in $(seq 1 40); do
  if /usr/bin/nc -z 127.0.0.1 "$PORT" >/dev/null 2>&1; then
    open_window "$URL"
    exit 0
  fi
  sleep 1
done

alert "启动超时。日志见：
$LOG"
exit 1
'''

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>JQ Quant</string>
  <key>CFBundleDisplayName</key><string>JQ Quant</string>
  <key>CFBundleIdentifier</key><string>ai.jqquant.terminal</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>jq-quant</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
"""


def main() -> int:
    out = {"kind": "make_jq_app"}

    macos = APP / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    (APP / "Contents" / "Info.plist").write_text(PLIST, encoding="utf-8")
    exe = macos / "jq-quant"
    exe.write_text(LAUNCHER, encoding="utf-8")
    exe.chmod(0o755)
    out["app"] = str(APP)

    # 图标：先看自己 Resources 里有没有（重复运行本脚本时会有），
    # 没有再去别的 app 里找一个来用。
    res = APP / "Contents" / "Resources"
    res.mkdir(parents=True, exist_ok=True)
    icon_path = next(iter(res.glob("*.icns")), None)
    if icon_path is None:
        for base in (ALL / "trade", ALL / "01_启动器"):
            if not base.exists():
                continue
            found = next(iter(base.rglob("*.icns")), None)
            if found:
                icon_path = res / "AppIcon.icns"
                shutil.copy2(found, icon_path)
                break
    if icon_path is not None:
        # Info.plist 每次都被重写，所以图标声明也要每次补回去，
        # 否则重跑本脚本会把图标声明弄丢（表现为图标文件在、却显示白板）。
        import plistlib
        plist = APP / "Contents" / "Info.plist"
        data = plistlib.loads(plist.read_bytes())
        data["CFBundleIconFile"] = icon_path.stem
        plist.write_bytes(plistlib.dumps(data))
        out["icon"] = icon_path.name

    # 收走桌面旧入口
    TRASH.mkdir(parents=True, exist_ok=True)
    moved = []
    for name in OLD_DESKTOP:
        src = DESKTOP / name
        if not src.exists():
            continue
        dst = TRASH / name
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True) if dst.is_dir() else dst.unlink()
        # 用系统 mv 而不是 shutil.move：桌面在 iCloud、All here 在本地盘时，
        # shutil 的 fcopyfile 快速路径会报 "Resource deadlock avoided"。
        # 先试 mv；跨卷（桌面在 iCloud、All here 在本地盘）时 rename 会失败，
        # 退回「复制再删除」，这两步在跨卷时是可靠的。
        r = subprocess.run(["/bin/mv", str(src), str(dst)], capture_output=True, text=True)
        if r.returncode != 0:
            r = subprocess.run(["/bin/cp", "-RP", str(src), str(dst)],
                               capture_output=True, text=True)
            if r.returncode == 0:
                subprocess.run(["/bin/rm", "-rf", str(src)], capture_output=True)
        if not src.exists():
            moved.append(name)
        else:
            out.setdefault("move_errors", []).append(f"{name}: {r.stderr.strip()[:70]}")
    out["moved_to_trash"] = moved

    # 让 Finder 刷新图标
    subprocess.run(["touch", str(APP)], capture_output=True)

    remaining = [p.name for p in DESKTOP.iterdir()
                 if p.suffix in (".app", ".command") and not p.name.startswith(".")]
    out["desktop_entries_now"] = remaining

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
