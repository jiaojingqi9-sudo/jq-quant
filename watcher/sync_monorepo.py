#!/usr/bin/env python3
"""sync_monorepo - 把两个系统的最新提交同步进 jq-quant 并推送。

首次用 subtree add 建立，之后用 subtree pull 增量同步。邮差与技能目录没有
自己的 git 历史，直接覆盖复制。
"""
import json, shutil, subprocess, sys
from pathlib import Path

HOME=Path.home(); ALL=HOME/"All here"; STAGE=ALL/".jq_quant_repo"
SUBTREES=[("trade",ALL/"trade"),("news-collector",ALL/"news collector")]
PLAIN=[("watcher",ALL/"futu_watcher"),("skills",ALL/"skills")]
SKIP={"__pycache__",".pytest_cache",".DS_Store",".git"}

def run(cmd,cwd=None,timeout=900):
    p=subprocess.run(cmd,cwd=str(cwd) if cwd else None,capture_output=True,text=True,timeout=timeout)
    return p.returncode,(p.stdout or "").strip(),(p.stderr or "").strip()

def copytree(src,dst):
    if dst.exists(): shutil.rmtree(dst)
    shutil.copytree(src,dst,ignore=lambda d,n:[x for x in n if x in SKIP or x.endswith(".pyc")])

def main():
    out={"kind":"sync_monorepo","steps":[]}
    if not STAGE.exists():
        out["error"]="暂存仓库不存在，先跑 build_monorepo"
        print(json.dumps(out,ensure_ascii=False,indent=2)); return 1

    for prefix,repo in SUBTREES:
        rc,branch,_=run(["git","-C",str(repo),"rev-parse","--abbrev-ref","HEAD"])
        rc,head,_=run(["git","-C",str(repo),"log","-1","--format=%h %s"])
        rc,so,se=run(["git","subtree","pull",f"--prefix={prefix}",str(repo),branch,
                      "-m",f"同步 {prefix}"],cwd=STAGE)
        out["steps"].append({"repo":prefix,"branch":branch,"head":head[:60],
                             "rc":rc,"detail":(so or se)[-160:]})

    for prefix,src in PLAIN:
        if src.exists():
            copytree(src,STAGE/prefix)
    run(["git","add","-A"],cwd=STAGE)
    rc,so,se=run(["git","-c","user.name=Jiao","-c","user.email=jiaojingqi9@gmail.com",
                  "commit","-m","同步邮差与技能脚本"],cwd=STAGE)
    out["steps"].append({"plain_commit_rc":rc,"detail":(so or se)[-120:]})

    rc,so,se=run(["git","push","origin","main"],cwd=STAGE,timeout=1800)
    out["push_rc"]=rc; out["push"]=(so+" "+se)[-300:]
    rc,so,_=run(["git","log","--oneline","-5"],cwd=STAGE); out["recent"]=so.splitlines()
    rc,so,_=run(["git","rev-list","--count","HEAD"],cwd=STAGE); out["total_commits"]=so
    print(json.dumps(out,ensure_ascii=False,indent=2)); return 0

sys.exit(main())
