#!/usr/bin/env python3
"""tradetests - 跑 trade 项目的单元测试。"""
import json, subprocess, sys
from pathlib import Path
TRADE = Path.home()/"All here"/"trade"
VENV = TRADE/".venv"/"bin"/"python"
py = str(VENV) if VENV.exists() else sys.executable
p = subprocess.run([py,"-m","pytest","-q","--no-header"],
                   cwd=str(TRADE), capture_output=True, text=True, timeout=900)
tail=(p.stdout or p.stderr).strip().splitlines()
print(json.dumps({"kind":"tradetests","returncode":p.returncode,
                  "passed":p.returncode==0,"tail":tail[-16:]},ensure_ascii=False,indent=2))
