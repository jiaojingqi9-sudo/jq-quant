#!/usr/bin/env python3
"""profile_live - 开启计时开关跑一次股票页，读出各阶段耗时。"""
import json, os, subprocess
from pathlib import Path
TRADE=Path.home()/"All here"/"trade"
VENV=TRADE/".venv"/"bin"/"python"
OUT=TRADE/"runtime"/"live_payload_timing.json"
if OUT.exists(): OUT.unlink()
code = r'''
import time, json
from streamlit.testing.v1 import AppTest
at=AppTest.from_file("src/taa_futu/dashboard_app.py", default_timeout=300)
at.run()
t0=time.time()
at.button(key="enter_stock").click().run()
print("TOTAL"+json.dumps({"total_sec":round(time.time()-t0,1),
                          "exceptions":[str(e)[:90] for e in at.exception][:2]}))
'''
env=dict(os.environ); env["JQ_PROFILE_LIVE"]="1"
p=subprocess.run([str(VENV),"-c",code],cwd=str(TRADE),capture_output=True,text=True,
                 timeout=900,env=env)
o=(p.stdout or "")+(p.stderr or "")
line=[l for l in o.splitlines() if l.startswith("TOTAL")]
res={"kind":"profile_live"}
res["run"]=json.loads(line[0][5:]) if line else {"raw":o[-500:]}
if OUT.exists():
    res["breakdown"]=json.loads(OUT.read_text(encoding="utf-8"))
else:
    res["breakdown"]="未生成（可能走了缓存或未进入该分支）"
print(json.dumps(res,ensure_ascii=False,indent=2))
