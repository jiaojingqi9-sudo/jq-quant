#!/usr/bin/env python3
"""baseline_test - 在改动前的提交上跑同一个测试，判断失败是否为我引入。

用 git worktree 检出旧提交到临时目录，不动当前工作区。
"""
import json, shutil, subprocess, sys, tempfile
from pathlib import Path
TRADE = Path.home()/"All here"/"trade"
VENV = TRADE/".venv"/"bin"/"python"
TEST = "tests/test_dashboard_e2e.py::test_click_enter_stock_switches_view"

def run(cmd, cwd=None, timeout=600):
    p=subprocess.run(cmd,cwd=str(cwd) if cwd else None,capture_output=True,text=True,timeout=timeout)
    return p.returncode,(p.stdout or "").strip(),(p.stderr or "").strip()

out={"kind":"baseline_test","test":TEST}
tmp = Path(tempfile.mkdtemp(prefix="jq_baseline_"))
wt = tmp/"wt"
try:
    # 3d5bf5d 是我做插件化之前的提交
    rc,so,se = run(["git","-C",str(TRADE),"worktree","add","--detach",str(wt),"3d5bf5d"])
    out["worktree_rc"]=rc
    if rc!=0:
        out["worktree_err"]=se[-300:]
    else:
        # 用同一个 venv 跑旧代码
        rc,so,se = run([str(VENV),"-m","pytest",TEST,"-q","--no-header","-p","no:warnings"],
                       cwd=wt, timeout=400)
        out["baseline_rc"]=rc
        out["baseline_passed"]= rc==0
        out["baseline_tail"]=((so or "")+(se or "")).strip().splitlines()[-6:]
finally:
    run(["git","-C",str(TRADE),"worktree","remove","--force",str(wt)])
    shutil.rmtree(tmp, ignore_errors=True)
print(json.dumps(out,ensure_ascii=False,indent=2))
