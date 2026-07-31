#!/usr/bin/env python3
"""baseline_realdata - 在真实数据目录下，用改动前的代码量股票页渲染耗时。

做法：把改动前的 4 个相关文件临时检出到工作区，测完立刻还原。
先 stash 当前改动保证不丢，测完 pop 回来。
"""
import json, subprocess, sys
from pathlib import Path
TRADE=Path.home()/"All here"/"trade"
VENV=TRADE/".venv"/"bin"/"python"
def git(*a,timeout=120):
    p=subprocess.run(["git","-C",str(TRADE),*a],capture_output=True,text=True,timeout=timeout)
    return p.returncode,(p.stdout or "").strip(),(p.stderr or "").strip()

out={"kind":"baseline_realdata"}
rc,so,se=git("stash","push","-u","-m","jq_baseline_probe")
out["stashed"]= rc==0 and "No local changes" not in so
out["stash_msg"]=(so or se)[:120]
try:
    code = r'''
import time, json
from streamlit.testing.v1 import AppTest
at=AppTest.from_file("src/taa_futu/dashboard_app.py", default_timeout=300)
t0=time.time(); at.run(); home=round(time.time()-t0,1)
t0=time.time(); at.button(key="enter_stock").click().run(); stock=round(time.time()-t0,1)
print("RES"+json.dumps({"home_sec":home,"stock_sec":stock,
                        "view":at.session_state["view"],
                        "exceptions":[str(e)[:90] for e in at.exception][:2]}))
'''
    p=subprocess.run([str(VENV),"-c",code],cwd=str(TRADE),capture_output=True,text=True,timeout=800)
    o=(p.stdout or "")+(p.stderr or "")
    line=[l for l in o.splitlines() if l.startswith("RES")]
    out["baseline_result"]=json.loads(line[0][3:]) if line else {"raw":o[-500:]}
finally:
    if out.get("stashed"):
        rc,so,se=git("stash","pop")
        out["restored"]= rc==0
        out["restore_msg"]=(so or se)[-200:]
print(json.dumps(out,ensure_ascii=False,indent=2))
