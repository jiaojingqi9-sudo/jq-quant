#!/usr/bin/env python3
"""launchd_audit - 盘点本机后台任务，并检查它们指向的文件是否还在。

为什么必须在本机跑：沙箱是独立的 Linux 虚拟机，`launchctl` 看不到用户 Mac 上
加载的任务。而「哪些启动器能删」这个判断，前提正是「没有后台任务在引用它」。

只读，不改任何东西。
"""
import json
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
ALL = HOME / "All here"


def sh(*args, timeout=30):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except Exception as exc:
        return -1, "", str(exc)


def main():
    out = {"kind": "launchd_audit"}

    # 1. 当前加载的任务（只看用户自己的，过滤掉系统与第三方 app）
    uid = os.getuid()
    rc, so, _ = sh("launchctl", "list")
    loaded = []
    for line in so.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        pid, status, label = parts[0], parts[1], parts[2]
        if not re.search(r"(taa|futu|market.?news|jq|quant|trade|watcher|codex)", label, re.I):
            continue
        loaded.append({"label": label, "pid": pid, "last_exit": status})
    out["loaded_jobs"] = loaded

    # 2. 已安装的 plist 文件，及其指向的程序是否存在
    agents = HOME / "Library" / "LaunchAgents"
    installed = []
    if agents.exists():
        for f in sorted(agents.glob("*.plist")):
            if not re.search(r"(taa|futu|market.?news|jq|quant|trade|watcher|codex)", f.name, re.I):
                continue
            entry = {"plist": f.name}
            try:
                data = plistlib.loads(f.read_bytes())
                entry["label"] = data.get("Label", "?")
                args = data.get("ProgramArguments") or ([data["Program"]] if "Program" in data else [])
                entry["args"] = args
                # 逐个参数看是不是路径，是的话检查存在性
                missing = [a for a in args
                           if isinstance(a, str) and a.startswith("/") and not Path(a).exists()]
                entry["missing_targets"] = missing
                entry["interval"] = data.get("StartInterval")
                cal = data.get("StartCalendarInterval")
                if cal:
                    entry["calendar"] = cal
                entry["run_at_load"] = bool(data.get("RunAtLoad"))
            except Exception as exc:
                entry["error"] = str(exc)[:120]
            installed.append(entry)
    out["installed_plists"] = installed

    # 3. 正在跑的相关进程
    rc, so, _ = sh("ps", "-Ao", "pid,etime,command")
    procs = []
    for line in so.splitlines():
        if re.search(r"(market_news|taa_futu|streamlit|watcher\.py|FutuOpenD)", line) and "grep" not in line:
            f = line.split(None, 2)
            if len(f) == 3:
                procs.append({"pid": f[0], "uptime": f[1], "cmd": f[2][:150]})
    out["running"] = procs

    # 4. 桌面上还有什么入口
    desk = HOME / "Desktop"
    out["desktop"] = sorted(p.name for p in desk.iterdir()
                            if p.suffix in (".app", ".command") and not p.name.startswith("."))

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
