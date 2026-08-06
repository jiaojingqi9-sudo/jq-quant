#!/usr/bin/env python3
"""tradetest_one - 跑单个测试并输出完整报错。"""
import json, subprocess, sys, os
from pathlib import Path
TRADE = Path.home()/"All here"/"trade"
VENV = TRADE/".venv"/"bin"/"python"
py = str(VENV) if VENV.exists() else sys.executable
# 目标优先级：环境变量 > 队列里的 _test_target.txt > 默认值。
# 加中间这一层是因为邮差 spawn 子进程时不透传环境变量，
# 沙箱侧只能写文件，没法设 env。
_TARGET_FILE = Path.home()/"All here"/"futu_queue"/"_test_target.txt"
def _from_file():
    try:
        return _TARGET_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None
target = (os.environ.get("JQ_TEST") or _from_file()
          or "tests/test_dashboard_e2e.py::test_click_enter_stock_switches_view")
# target 允许带额外 flag（如 "tests/ --collect-only"），按空格拆开传给 pytest
p = subprocess.run([py,"-m","pytest",*target.split(),"-q","--no-header","-x","--tb=long","-p","no:warnings"],
                   cwd=str(TRADE), capture_output=True, text=True, timeout=600)
out=(p.stdout or "")+(p.stderr or "")
print(json.dumps({"kind":"tradetest_one","target":target,"rc":p.returncode,
                  "output":out[-3000:]},ensure_ascii=False,indent=2))
