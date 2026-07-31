#!/usr/bin/env python3
"""doctor_test - 单独跑 stock_doctor 与账本相关的测试，拿快反馈。"""
import json
import subprocess
import sys
from pathlib import Path

TRADE = Path.home() / "All here" / "trade"
PY = TRADE / ".venv" / "bin" / "python"


def main() -> int:
    p = subprocess.run(
        [str(PY), "-m", "pytest", "-q", "--no-header", "-p", "no:warnings",
         "tests/test_stock_doctor.py", "tests/test_stock_ledger.py",
         "tests/test_stock_runtime.py", "tests/test_auto_trader.py"],
        cwd=str(TRADE), capture_output=True, text=True, timeout=900)
    text = ((p.stdout or "") + (p.stderr or "")).splitlines()
    print(json.dumps({
        "kind": "doctor_test", "rc": p.returncode,
        "summary": [l for l in text[-14:] if l.strip()],
        "failures": [l for l in text if l.startswith(("FAILED", "ERROR"))][:12],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
