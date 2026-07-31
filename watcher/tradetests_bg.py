#!/usr/bin/env python3
"""tradetests_bg - 后台跑 trade 测试套件，结果写入文件。"""
import json, subprocess, sys, time
from pathlib import Path
TRADE=Path.home()/"All here"/"trade"
VENV=TRADE/".venv"/"bin"/"python"
RESULT=TRADE/"runtime"/"pytest_result.json"

def main():
    if "--run" in sys.argv:
        py=str(VENV) if VENV.exists() else sys.executable
        RESULT.parent.mkdir(parents=True,exist_ok=True)
        RESULT.write_text(json.dumps({"running":True,"started":time.strftime("%H:%M:%S")}),encoding="utf-8")
        t0=time.time()
        p=subprocess.run([py,"-m","pytest","-q","--no-header","-p","no:warnings"],
                         cwd=str(TRADE),capture_output=True,text=True,timeout=3600)
        out=((p.stdout or "")+(p.stderr or "")).strip().splitlines()
        RESULT.write_text(json.dumps({
            "running":False,"returncode":p.returncode,"passed":p.returncode==0,
            "elapsed_sec":int(time.time()-t0),
            "summary":[l for l in out if "passed" in l or "failed" in l][-3:],
            "failures":[l for l in out if l.startswith("FAILED")][:10],
        },ensure_ascii=False,indent=2),encoding="utf-8")
        return 0
    # 启动器
    if subprocess.run(["pgrep","-f","tradetests_bg.py --run"],capture_output=True,text=True).stdout.strip():
        print(json.dumps({"kind":"tradetests_bg","status":"已在运行"},ensure_ascii=False)); return 0
    log=TRADE/"runtime"/"pytest_bg.log"
    with open(log,"ab") as f:
        subprocess.Popen([sys.executable,str(Path(__file__)),"--run"],
                         stdout=f,stderr=f,start_new_session=True)
    print(json.dumps({"kind":"tradetests_bg","status":"已后台启动","result_file":str(RESULT)},ensure_ascii=False))
    return 0

sys.exit(main())
