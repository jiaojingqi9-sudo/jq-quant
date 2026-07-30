#!/usr/bin/env python3
"""ghrepos - 列出账号下的仓库（只读）。"""
import json, subprocess, sys
GH="/opt/homebrew/bin/gh"
def run(*a, timeout=40):
    p=subprocess.run([GH,*a],capture_output=True,text=True,timeout=timeout)
    return p.returncode,(p.stdout or "").strip(),(p.stderr or "").strip()
out={"kind":"ghrepos"}
rc,so,se=run("api","user","--jq",".login")
out["account"]=so or se[:120]
rc,so,se=run("repo","list","--limit","30","--json","name,isPrivate,url,createdAt,diskUsage")
if rc==0 and so:
    try:
        repos=json.loads(so)
        out["repos"]=[{"name":r["name"],"private":r["isPrivate"],"url":r["url"],
                       "created":r["createdAt"][:10],"KB":r.get("diskUsage")} for r in repos]
    except Exception as e:
        out["parse_error"]=str(e); out["raw"]=so[:400]
else:
    out["error"]=se[:200]
print(json.dumps(out,ensure_ascii=False,indent=2))
