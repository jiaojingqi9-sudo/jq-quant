#!/usr/bin/env python3
"""apptest_timing - 用宽松超时量真实渲染耗时，判断 30 秒上限是否本就不够。"""
import json, subprocess
from pathlib import Path
TRADE=Path.home()/"All here"/"trade"
VENV=TRADE/".venv"/"bin"/"python"
code = r'''
import time, json, sys
from streamlit.testing.v1 import AppTest
res={}
t0=time.time()
at=AppTest.from_file("src/taa_futu/dashboard_app.py", default_timeout=180)
at.run()
res["home_render_sec"]=round(time.time()-t0,1)
res["home_view"]=at.session_state["view"]
res["home_buttons"]=[b.key for b in at.button][:10]
res["home_exceptions"]=[str(e)[:100] for e in at.exception][:3]

t0=time.time()
at.button(key="enter_stock").click().run()
res["stock_render_sec"]=round(time.time()-t0,1)
res["stock_view"]=at.session_state["view"]
res["stock_exceptions"]=[str(e)[:120] for e in at.exception][:3]
print("RES"+json.dumps(res))
'''
p=subprocess.run([str(VENV),"-c",code],cwd=str(TRADE),capture_output=True,text=True,timeout=900)
out=(p.stdout or "")+(p.stderr or "")
line=[l for l in out.splitlines() if l.startswith("RES")]
print(json.dumps({"kind":"apptest_timing",
                  "result": json.loads(line[0][3:]) if line else {"raw":out[-800:]}},
                 ensure_ascii=False,indent=2))
