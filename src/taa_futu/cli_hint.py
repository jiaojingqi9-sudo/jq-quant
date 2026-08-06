"""界面上要提示「去敲哪条命令」时，生成当前这台机器能直接粘贴运行的写法。

以前各处都写死 ``.venv/bin/taa-futu ...``。Windows 上 venv 的可执行文件在
``.venv\\Scripts\\`` 下，照着界面上的命令敲会报「系统找不到指定的路径」——
2026-08-06 在一台 Windows 机器上照 README 部署时踩到的。

单独成一个模块而不是留在 stock_doctor 里：dashboard、crypto 面板、doctor
三处都要拼这种提示，各写一份迟早又会漏掉一处。
"""
from __future__ import annotations

import os
from pathlib import Path
import sys


def venv_command(subcommand: str, *, exe: str | None = None, on_windows: bool | None = None) -> str:
    """``taa-futu <subcommand>`` 在这台机器上的完整写法。

    优先用正在跑这段代码的解释器所在目录（venv 不叫 .venv 也对），
    找不到再按平台给默认值。

    ``exe`` / ``on_windows`` 只给测试用——直接改全局 ``os.name`` 会把 pathlib
    一起带偏（``Path()`` 按 ``os.name`` 选 WindowsPath/PosixPath）。
    """
    if on_windows is None:
        on_windows = os.name == "nt"
    name = "taa-futu.exe" if on_windows else "taa-futu"
    exe_dir = Path(exe or sys.executable).parent
    # venv 里 python 和 taa-futu 同目录；系统级安装时脚本在 Scripts/ 或 bin/ 下
    base = ".venv\\Scripts\\taa-futu" if on_windows else ".venv/bin/taa-futu"
    for folder in (exe_dir, exe_dir / ("Scripts" if on_windows else "bin")):
        script = folder / name
        if not script.exists():
            continue
        try:
            base = str(script.relative_to(Path.cwd()))
        except ValueError:
            base = str(script)
        if on_windows and base.lower().endswith(".exe"):
            base = base[:-4]
        break
    return f"{base} {subcommand}"
