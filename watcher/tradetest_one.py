#!/usr/bin/env python3
"""tradetest_one - 跑单个测试并输出完整报错。"""
import json, subprocess, sys, os
from pathlib import Path
TRADE = Path.home()/"All here"/"trade"
VENV = TRADE/".venv"/"bin"/"python"
py = str(VENV) if VENV.exists() else sys.executable
target = os.environ.get("JQ_TEST","tests/test_dashboard_e2e.py::test_click_enter_stock_switches_view")
p = subprocess.run([py,"-m","pytest",target,"-q","--no-header","-x","--tb=long","-p","no:warnings"],
                   cwd=str(TRADE), capture_output=True, text=True, timeout=600)
out=(p.stdout or "")+(p.stderr or "")
print(json.dumps({"kind":"tradetest_one","target":target,"rc":p.returncode,
                  "output":out[-3000:]},ensure_ascii=False,indent=2))
