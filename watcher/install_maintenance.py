#!/usr/bin/env python3
"""install_maintenance - 装一个每周自动清理的 launchd 任务。

为什么需要：日志与历史文件没有轮转。2026-07-30 手工清过一次
（1.3GB → 55MB），几小时后就回涨到 69MB。不装自动任务，几个月后又是 GB 级。

任务做什么：调用已验证过的 logclean --apply，历史文件各留最近 3000 条、
日志各留最近 5MB。数据库、行情数据、交易记录一律不碰。

时间：每周日凌晨 3 点。避开交易时段与采集高峰。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
LABEL = "ai.jqquant.maintenance"
PLIST = HOME / "Library" / "LaunchAgents" / f"{LABEL}.plist"
SCRIPT = HOME / "All here" / "futu_watcher" / "logclean.py"
LOG_DIR = HOME / "All here" / "news collector" / "runtime" / "logs"

PLIST_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>{SCRIPT}</string>
    <string>--apply</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>{HOME}</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key><integer>0</integer>
    <key>Hour</key><integer>3</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key><string>{LOG_DIR}/maintenance.log</string>
  <key>StandardErrorPath</key><string>{LOG_DIR}/maintenance.err.log</string>
</dict>
</plist>
"""


def run(cmd, timeout=30):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def main():
    out = {"kind": "install_maintenance", "label": LABEL}
    uid = os.getuid()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(PLIST_XML, encoding="utf-8")
    out["plist"] = str(PLIST)

    run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"])
    rc, so, se = run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)])
    out["bootstrap_rc"] = rc
    if se:
        out["bootstrap_err"] = se[:200]

    rc, so, _ = run(["launchctl", "list"])
    out["loaded"] = any(LABEL in l for l in so.splitlines())
    out["schedule"] = "每周日 03:00"
    out["action"] = "history/*.jsonl 各留最近 3000 条；runtime/logs/*.log 各留最近 5MB"
    out["untouched"] = "数据库、行情数据、交易记录不碰"

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
