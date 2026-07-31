#!/usr/bin/env python3
"""newstests - 跑 news collector 的测试（与 cookie / 采集器 / 报告相关的那部分）。"""
import json
import subprocess
import sys
from pathlib import Path

NEWS = Path.home() / "All here" / "news collector"
PY = "/opt/anaconda3/bin/python3"


def main() -> int:
    out = {"kind": "newstests"}
    p = subprocess.run(
        [PY, "-m", "pytest", "-q", "--no-header", "-p", "no:warnings",
         "-k", "cookie or collector or reporting or notification"],
        cwd=str(NEWS), capture_output=True, text=True, timeout=1500)
    text = ((p.stdout or "") + (p.stderr or "")).splitlines()
    out["rc"] = p.returncode
    out["summary"] = [l for l in text[-20:] if l.strip()]
    out["failures"] = [l for l in text if l.startswith(("FAILED", "ERROR"))][:15]
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
