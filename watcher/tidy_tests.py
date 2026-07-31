#!/usr/bin/env python3
"""tidy_tests - 跑与「移走启动器」直接相关的测试。

改动集中在两个文件：unified_panel.py 去掉三个按钮、dashboard_extras.py 去掉
一个按钮。这四个测试文件正好覆盖它们，加上首页与端到端两个，确认导航没坏。

跑全量 455 个要 26 分钟，这里只跑相关的，先拿到快反馈。
"""
import json
import subprocess
import sys
from pathlib import Path

TRADE = Path.home() / "All here" / "trade"
VENV = TRADE / ".venv" / "bin" / "python"
PY = str(VENV) if VENV.exists() else sys.executable

TARGETS = [
    "tests/test_unified_panel.py",
    "tests/test_dashboard_extras.py",
    "tests/test_control_panel.py",
    "tests/test_dashboard_controls.py",
    "tests/test_plugin.py",
    "tests/test_shell_home.py",
]


def main():
    out = {"kind": "tidy_tests", "results": {}}
    existing = [t for t in TARGETS if (TRADE / t).exists()]
    out["skipped_missing"] = [t for t in TARGETS if t not in existing]

    p = subprocess.run(
        [PY, "-m", "pytest", *existing, "-q", "--no-header", "-p", "no:warnings"],
        cwd=str(TRADE), capture_output=True, text=True, timeout=1800)
    text = (p.stdout or "") + (p.stderr or "")
    out["rc"] = p.returncode
    # 只留最后的汇总行与失败摘要
    lines = text.splitlines()
    out["summary"] = [l for l in lines[-25:] if l.strip()]
    fails = [l for l in lines if l.startswith("FAILED") or l.startswith("ERROR")]
    out["failures"] = fails[:20]
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
