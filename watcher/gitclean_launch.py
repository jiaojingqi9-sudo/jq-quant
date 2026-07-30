#!/usr/bin/env python3
"""gitclean_launch - 后台跑 gitclean，避免 watcher 超时。"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path.home() / "All here" / "futu_watcher" / "gitclean.py"
LOG = Path.home() / "All here" / "news collector" / "runtime" / "gitclean.log"
PROGRESS = Path.home() / "All here" / "news collector" / "runtime" / "gitclean_progress.json"


def main():
    out = {"kind": "gitclean_launch"}
    p = subprocess.run(["pgrep", "-f", "gitclean.py --run"], capture_output=True, text=True)
    if p.stdout.strip():
        out["status"] = "已在运行"
    else:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "ab") as log:
            subprocess.Popen([sys.executable, str(SCRIPT), "--run"],
                             stdout=log, stderr=log, start_new_session=True)
        out["status"] = "已后台启动"
    out["progress_file"] = str(PROGRESS)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
