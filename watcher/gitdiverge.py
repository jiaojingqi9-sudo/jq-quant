#!/usr/bin/env python3
"""gitdiverge - 判断本地仓库与 7/24 远程仓库的分叉情况（只读，不改动任何东西）。"""
import json, subprocess
from pathlib import Path
HOME=Path.home()
PAIRS={
 "trade": (HOME/"All here"/"trade", "https://github.com/jiaojingqi9-sudo/quant-trading-workbench.git"),
 "news collector": (HOME/"All here"/"news collector", "https://github.com/jiaojingqi9-sudo/market-news-collector.git"),
}
def git(repo,*a,timeout=120):
    p=subprocess.run(["git","-C",str(repo),*a],capture_output=True,text=True,timeout=timeout)
    return p.returncode,(p.stdout or "").strip(),(p.stderr or "").strip()
out={"kind":"gitdiverge","repos":{}}
for name,(repo,url) in PAIRS.items():
    e={"remote_url":url}
    # 临时抓取远程到一个一次性 ref，不改变任何本地分支
    rc,so,se=git(repo,"fetch","--no-tags",url,"main:refs/tmp/remote_main","--force")
    e["fetch_ok"]= rc==0
    if rc!=0:
        e["fetch_error"]=se[-200:]; out["repos"][name]=e; continue
    _,head,_=git(repo,"rev-parse","HEAD")
    _,rhead,_=git(repo,"rev-parse","refs/tmp/remote_main")
    e["local_head"]=head[:8]; e["remote_head"]=rhead[:8]
    # 共同祖先
    rc,base,_=git(repo,"merge-base","HEAD","refs/tmp/remote_main")
    e["common_ancestor"]=base[:8] if rc==0 else "(无共同历史)"
    if rc==0:
        _,ahead,_=git(repo,"rev-list","--count",f"{base}..HEAD")
        _,behind,_=git(repo,"rev-list","--count",f"{base}..refs/tmp/remote_main")
        e["local_ahead"]=int(ahead or 0); e["remote_ahead"]=int(behind or 0)
        _,lc,_=git(repo,"log","--oneline",f"{base}..HEAD")
        _,rc2,_=git(repo,"log","--oneline",f"{base}..refs/tmp/remote_main")
        e["local_only_commits"]=lc.splitlines()[:5]
        e["remote_only_commits"]=rc2.splitlines()[:5]
        # 远程独有的提交改了哪些文件
        _,files,_=git(repo,"diff","--name-only",f"{base}..refs/tmp/remote_main")
        e["remote_changed_files"]=files.splitlines()[:12]
        e["remote_changed_count"]=len(files.splitlines())
    git(repo,"update-ref","-d","refs/tmp/remote_main")
    out["repos"][name]=e
print(json.dumps(out,ensure_ascii=False,indent=2))
