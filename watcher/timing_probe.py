#!/usr/bin/env python3
"""timing_probe - 量化插件化引入的开销。"""
import json, subprocess, sys
from pathlib import Path
TRADE=Path.home()/"All here"/"trade"
VENV=TRADE/".venv"/"bin"/"python"
code = r'''
import time, sys
sys.path.insert(0,"src")
t={}
t0=time.time()
from taa_futu.plugin import registry
t["import_plugin"]=round(time.time()-t0,3)

t0=time.time()
registry.discover()
t["discover"]=round(time.time()-t0,3)
t["features"]=len(registry.all())

# 每个功能的可用性检查耗时（外壳每次渲染都会调）
per={}
for f in registry.all():
    t1=time.time(); f.availability(); per[f.id]=round(time.time()-t1,4)
t["availability_each"]=per
t["availability_total"]=round(sum(per.values()),4)

t0=time.time()
import taa_futu.shell
t["import_shell"]=round(time.time()-t0,3)
import json as j; print("TIMING"+j.dumps(t))
'''
p=subprocess.run([str(VENV),"-c",code],cwd=str(TRADE),capture_output=True,text=True,timeout=300)
out=(p.stdout or "")+(p.stderr or "")
line=[l for l in out.splitlines() if l.startswith("TIMING")]
res=json.loads(line[0][6:]) if line else {"raw":out[-600:]}
print(json.dumps({"kind":"timing_probe","result":res},ensure_ascii=False,indent=2))
