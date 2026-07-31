#!/usr/bin/env python3
"""finish_desktop - 收走最后一个桌面旧入口，并给 JQ Quant.app 配图标。

mv 与 cp 在 iCloud 桌面上都可能因扩展属性失败；对小脚本直接「读内容→写新文件→
删原件」最可靠。
"""
import json, plistlib, shutil, subprocess, sys
from pathlib import Path
HOME=Path.home(); ALL=HOME/"All here"; DESKTOP=HOME/"Desktop"
APP=DESKTOP/"JQ Quant.app"; TRASH=ALL/"_回收站_20260730"/"旧桌面入口"
out={"kind":"finish_desktop"}

# 1. 收走残留的 .command
TRASH.mkdir(parents=True, exist_ok=True)
src=DESKTOP/"启动量化交易控制台.command"
if src.exists():
    try:
        data=src.read_bytes()
        (TRASH/src.name).write_bytes(data)
        subprocess.run(["/bin/rm","-f",str(src)],capture_output=True)
        out["moved"]= not src.exists()
        out["bytes"]=len(data)
    except Exception as e:
        out["move_error"]=str(e)[:120]
else:
    out["moved"]="已不在桌面"

# 2. 找图标
found=None
for base in (ALL/"trade", ALL/"01_启动器", ALL/"02_系统文档"):
    if base.exists():
        for p in base.rglob("*.icns"):
            found=p; break
    if found: break
if found:
    res=APP/"Contents"/"Resources"; res.mkdir(parents=True,exist_ok=True)
    shutil.copy2(found,res/"AppIcon.icns")
    pl=APP/"Contents"/"Info.plist"
    d=plistlib.loads(pl.read_bytes())
    d["CFBundleIconFile"]="AppIcon"
    pl.write_bytes(plistlib.dumps(d))
    out["icon"]=str(found.name)
else:
    out["icon"]="未找到 .icns，将显示系统默认图标"

subprocess.run(["touch",str(APP)],capture_output=True)
out["desktop_now"]=[p.name for p in DESKTOP.iterdir()
                    if p.suffix in (".app",".command") and not p.name.startswith(".")]
out["app_executable_ok"]=(APP/"Contents"/"MacOS"/"jq-quant").exists()
print(json.dumps(out,ensure_ascii=False,indent=2))
