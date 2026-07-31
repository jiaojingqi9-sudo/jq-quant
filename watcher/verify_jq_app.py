#!/usr/bin/env python3
"""verify_jq_app - 确认 app 用的是应用窗口模式，并检查图标。"""
import json, plistlib, subprocess
from pathlib import Path
# app 已从 JQ Quant 改名为寻宝猫，两个名字都认，免得改名后校验假报错。
DESKTOP=Path.home()/"Desktop"
APP=next((DESKTOP/n for n in ("寻宝猫.app","JQ Quant.app") if (DESKTOP/n).exists()),
         DESKTOP/"寻宝猫.app")
exe=APP/"Contents"/"MacOS"/"jq-quant"
out={"kind":"verify_jq_app","app":APP.name}
if exe.exists():
    txt=exe.read_text(encoding="utf-8")
    out["uses_app_window"]="--app=" in txt
    out["open_window_calls"]=txt.count('open_window "$URL"')
    out["bare_open_calls"]=txt.count('\n  open "$URL"')
    out["browsers_tried"]=[b for b in ("Google Chrome","Microsoft Edge","Brave Browser") if b in txt]
    r=subprocess.run(["/bin/zsh","-n",str(exe)],capture_output=True,text=True)
    out["syntax_ok"]=r.returncode==0
else:
    out["error"]="可执行文件不存在"
pl=APP/"Contents"/"Info.plist"
if pl.exists():
    d=plistlib.loads(pl.read_bytes())
    out["icon_declared"]=d.get("CFBundleIconFile","(未声明)")
    res=APP/"Contents"/"Resources"
    out["icon_files"]=[p.name for p in res.glob("*.icns")] if res.exists() else []
out["chrome_installed"]=Path("/Applications/Google Chrome.app").exists()
out["profile_dir"]=(Path.home()/"All here"/"trade"/"runtime"/"chrome_app_profile").exists()
print(json.dumps(out,ensure_ascii=False,indent=2))
