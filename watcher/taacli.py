#!/usr/bin/env python3
"""taacli - 在本机跑 trade 的 taa-futu 子命令。

放在本机跑：沙箱和挂载的 Linux VM 都连不到 OpenD，也没有 trade 的 venv
（那是 macOS 的二进制）。只有邮差这个原生进程能跑。

命令从队列文件读，一行一条：

    futu_queue/_taacli.txt

**只放行白名单里的子命令。** 邮差是文件队列，谁能往队列里写文件谁就能触发
这个 skill；不设白名单等于给了一条下单通道。所以 paper-trade、flatten-all、
cancel-orders 这类有下单/撤单副作用的一律不放，*-reset 这类会覆盖账本起点的
不可逆操作也不放——那种事应该人自己在终端敲，看着输出确认。

用法（邮差）：
    {"skill": "taacli"}
"""
import json
import subprocess
import sys
from pathlib import Path

TRADE = Path.home() / "All here" / "trade"
VENV = TRADE / ".venv" / "bin" / "taa-futu"
CMD_FILE = Path.home() / "All here" / "futu_queue" / "_taacli.txt"

# 只读、或只写自己那份报告文件的子命令
ALLOWED = {
    "stock-system-doctor",
    "stock-system-epoch",
    "stock-ledger-status",
    "stock-ledger-audit",
    "stock-learning-status",
    "stock-learning-build",
    "stock-learning-export",
    "stock-market-log-status",
    "signals",
    "live-signal",
    "real-check",
    "backtest",
}


def main() -> int:
    out = {"kind": "taacli", "runs": []}
    if not VENV.exists():
        out["error"] = f"找不到 {VENV}"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1
    try:
        lines = [l.strip() for l in CMD_FILE.read_text(encoding="utf-8").splitlines()]
    except OSError as exc:
        out["error"] = f"读不到命令文件 {CMD_FILE}: {exc}"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    for line in [l for l in lines if l and not l.startswith("#")]:
        parts = line.split()
        entry = {"command": line}
        if parts[0] not in ALLOWED:
            entry["error"] = f"子命令 {parts[0]} 不在白名单里；白名单：{sorted(ALLOWED)}"
            out["runs"].append(entry)
            continue
        p = subprocess.run([str(VENV), *parts], cwd=str(TRADE),
                           capture_output=True, text=True, timeout=900)
        entry["rc"] = p.returncode
        entry["output"] = ((p.stdout or "") + (p.stderr or ""))[-2500:]
        out["runs"].append(entry)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
