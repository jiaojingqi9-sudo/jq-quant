#!/usr/bin/env python3
"""archive_launch - 在后台启动 archive_marketdata，避免 watcher 180 秒超时。

压缩 131GB 大约要 40 分钟，远超 watcher 的单任务超时，所以用 nohup 丢到后台，
进度写到 runtime/archive_progress.json，随时可以用 archive_status 查看。
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path.home() / "All here" / "futu_watcher" / "archive_marketdata.py"
PROGRESS = Path.home() / "All here" / "trade" / "runtime" / "archive_progress.json"
LOG = Path.home() / "All here" / "trade" / "runtime" / "archive_marketdata.log"


def already_running():
    try:
        p = subprocess.run(["pgrep", "-f", "archive_marketdata.py --run"],
                           capture_output=True, text=True, timeout=10)
        return bool(p.stdout.strip())
    except Exception:
        return False


def main():
    out = {"kind": "archive_launch"}
    if already_running():
        out["status"] = "已在运行中，未重复启动"
    else:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "ab") as log:
            subprocess.Popen(
                [sys.executable, str(SCRIPT), "--run"],
                stdout=log, stderr=log, start_new_session=True,
            )
        out["status"] = "已在后台启动"
    out["progress_file"] = str(PROGRESS)
    out["log"] = str(LOG)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
