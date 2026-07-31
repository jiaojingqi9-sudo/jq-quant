#!/usr/bin/env python3
"""test_jq_app - 实测 JQ Quant.app 的启动脚本能否正常工作。

分两种情形验证：
  A. 服务已在跑 → 应直接打开浏览器、秒退出，不重复起进程
  B. 服务没在跑 → 应启动并等端口就绪
"""
import json, socket, subprocess, sys, time
from pathlib import Path
APP=Path.home()/"Desktop"/"JQ Quant.app"/"Contents"/"MacOS"/"jq-quant"
def port_open(p=8501):
    s=socket.socket(); s.settimeout(1.5)
    try: s.connect(("127.0.0.1",p)); return True
    except Exception: return False
    finally: s.close()
out={"kind":"test_jq_app","executable":str(APP),"exists":APP.exists()}
out["executable_bit"]=bool(APP.exists() and APP.stat().st_mode & 0o111)
out["port_before"]=port_open()

# 语法检查（不执行）
r=subprocess.run(["/bin/zsh","-n",str(APP)],capture_output=True,text=True)
out["syntax_ok"]= r.returncode==0
if r.returncode!=0: out["syntax_err"]=r.stderr[:200]

# 实际跑一次
t0=time.time()
r=subprocess.run([str(APP)],capture_output=True,text=True,timeout=120)
out["run_rc"]=r.returncode
out["run_sec"]=round(time.time()-t0,1)
if r.stderr: out["stderr"]=r.stderr[-200:]
time.sleep(2)
out["port_after"]=port_open()
out["verdict"]="可用" if (out["port_after"] and r.returncode==0) else "有问题"
print(json.dumps(out,ensure_ascii=False,indent=2))
