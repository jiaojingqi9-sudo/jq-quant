#!/usr/bin/env python3
"""profile_direct - 直接给 render_live_monitor 做 profiling，绕开 AppTest。

上一次 profiling 抓到的全是 AppTest 主线程的等待（time.sleep 占 53.5/55 秒），
真正的渲染在脚本线程里。这里用假 streamlit 直接调渲染函数，让耗时落在本线程，
profiler 才看得见。
"""
import json, subprocess
from pathlib import Path
TRADE=Path.home()/"All here"/"trade"
VENV=TRADE/".venv"/"bin"/"python"
code = r'''
import sys, types, time, cProfile, pstats, io, json, datetime
sys.path.insert(0,"src")

class Ctx:
    def __enter__(self): return self
    def __exit__(self,*a): return False
def noop(*a,**k): return Ctx()

class Col(Ctx):
    def __getattr__(self,n):
        return _widget(n)

def _widget(name):
    """按控件名返回合理类型，否则被测代码会在类型上炸掉，测不出真实耗时。"""
    def fn(*a,**k):
        if name in ("number_input","slider"): return k.get("value", 0) or 0
        if name in ("text_input","text_area"): return ""
        if name in ("checkbox","toggle","button"): return False
        if name=="multiselect": return []
        if name=="selectbox": 
            opts=k.get("options") or (a[1] if len(a)>1 else None)
            try: return list(opts)[0]
            except Exception: return None
        if name=="date_input": return datetime.date.today()
        if name in ("columns",):
            spec=a[0] if a else 2
            return [Col() for _ in (range(spec) if isinstance(spec,int) else spec)]
        if name=="tabs": return [Ctx() for _ in (a[0] if a else [1])]
        return Ctx()
    return fn

class FakeSt(types.ModuleType):
    def __getattr__(self,n):
        if n.startswith("__"): raise AttributeError(n)
        f=_widget(n); setattr(self,n,f); return f

st=FakeSt("streamlit")
st.session_state={}
st.sidebar=FakeSt("sidebar")
st.cache_data=lambda *a,**k:(lambda f:f)
st.cache_resource=lambda *a,**k:(lambda f:f)
comp=types.ModuleType("streamlit.components.v1"); comp.html=noop; comp.iframe=noop
st.components=types.SimpleNamespace(v1=comp)
sys.modules["streamlit"]=st
sys.modules["streamlit.components"]=types.ModuleType("streamlit.components")
sys.modules["streamlit.components.v1"]=comp

from taa_futu.config import load_settings
settings=load_settings()
from taa_futu.dashboard_app import render_live_monitor

pr=cProfile.Profile(); pr.enable(); t0=time.time()
try: render_live_monitor(settings)
except Exception as e: err=f"{type(e).__name__}: {e}"
else: err=None
el=time.time()-t0; pr.disable()
s=io.StringIO(); pstats.Stats(pr,stream=s).sort_stats("cumulative").print_stats(30)
rows=[l for l in s.getvalue().splitlines() if "All here" in l or "site-packages" in l][:22]
print("PROF"+json.dumps({"elapsed":round(el,1),"error":err,"top":rows}))
'''
p=subprocess.run([str(VENV),"-c",code],cwd=str(TRADE),capture_output=True,text=True,timeout=600)
o=(p.stdout or "")+(p.stderr or "")
line=[l for l in o.splitlines() if l.startswith("PROF")]
print(json.dumps({"kind":"profile_direct",
                  "result": json.loads(line[0][4:]) if line else {"raw":o[-1000:]}},
                 ensure_ascii=False,indent=2))
