#!/usr/bin/env python3
"""restore_from_trash - 把误判移走的启动器放回原处。

2026-07-31 的整理里有两个判断错了：

  Open_Stock_Screener.command
      我当成「app 里已有选股器页所以冗余」。实际上它启动的是
      futu_stock_screener_desktop.py —— 一个独立的 Tkinter 桌面窗口，
      和 Streamlit 里的选股器不是同一个东西，后者也没法嵌入前者。
      app 的选股器页里就有一个按钮专门开它。

  Open_Crypto_OFIM_App.command
      加密页在嵌入渲染失败时，会显示「打开独立 App」作为兜底，指的就是它。
      兜底路径本来就是给「主路径坏了」准备的，不能因为主路径现在好用就删。
"""
import json
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
ALL = HOME / "All here"
TRASH = ALL / "_回收站_20260730" / "冗余启动器_20260731"

RESTORE = [
    ("launchers", "Open_Stock_Screener.command", ALL / "trade" / "stock" / "launchers"),
    ("launchers", "Open_Crypto_OFIM_App.command", ALL / "trade" / "crypto" / "launchers"),
]


def main():
    out = {"kind": "restore_from_trash", "restored": [], "not_found": [], "failed": []}
    for sub, name, dest_dir in RESTORE:
        # 移走时按父目录名分的子文件夹，stock 和 crypto 的父目录都叫 launchers
        src = TRASH / sub / name
        if not src.exists():
            hits = list(TRASH.rglob(name))
            if not hits:
                out["not_found"].append(name)
                continue
            src = hits[0]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dst = dest_dir / name
        r = subprocess.run(["/bin/mv", str(src), str(dst)], capture_output=True, text=True)
        if r.returncode != 0:
            out["failed"].append({"name": name, "err": r.stderr.strip()[:120]})
            continue
        dst.chmod(0o755)
        out["restored"].append(str(dst.relative_to(ALL)))

    # 回收站里还剩哪些启动器。只看 .command，不要 rglob 整个回收站——
    # 那里面有刚移进来的 .venv，几千个文件会把输出撑爆。
    out["launchers_still_in_trash"] = sorted(
        str(p.relative_to(TRASH)) for p in TRASH.rglob("*.command"))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
