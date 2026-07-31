#!/usr/bin/env python3
"""profile_stock - 用 cProfile 定位股票页渲染的真实耗时点。

不靠猜：直接跑一次渲染并按累计耗时排序，看时间花在哪些函数上。
"""
import json, subprocess
from pathlib import Path
TRADE=Path.home()/"All here"/"trade"
VENV=TRADE/".venv"/"bin"/"python"
code = r'''
import cProfile, pstats, io, json, time
from streamlit.testing.v1 import AppTest

at = AppTest.from_file("src/taa_futu/dashboard_app.py", default_timeout=300)
at.run()                      # 先渲染首页（快），不计入
pr = cProfile.Profile()
pr.enable()
t0 = time.time()
at.button(key="enter_stock").click().run()
elapsed = time.time() - t0
pr.disable()

s = io.StringIO()
ps = pstats.Stats(pr, stream=s).sort_stats("cumulative")
ps.print_stats(28)
lines = [l for l in s.getvalue().splitlines() if l.strip()]
# 只保留有意义的行
keep = [l for l in lines if "{" not in l or "built-in" in l][:34]
print("PROF" + json.dumps({"elapsed": round(elapsed,1), "top": keep}))
'''
p=subprocess.run([str(VENV),"-c",code],cwd=str(TRADE),capture_output=True,text=True,timeout=900)
o=(p.stdout or "")+(p.stderr or "")
line=[l for l in o.splitlines() if l.startswith("PROF")]
print(json.dumps({"kind":"profile_stock",
                  "result": json.loads(line[0][4:]) if line else {"raw":o[-900:]}},
                 ensure_ascii=False,indent=2))
