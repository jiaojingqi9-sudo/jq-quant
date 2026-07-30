#!/usr/bin/env python3
"""ghinspect - 查看远程仓库的内容概况（只读）。"""
import json, subprocess
GH="/opt/homebrew/bin/gh"
def run(*a, timeout=40):
    p=subprocess.run([GH,*a],capture_output=True,text=True,timeout=timeout)
    return p.returncode,(p.stdout or "").strip(),(p.stderr or "").strip()
out={"kind":"ghinspect","repos":{}}
for name in ("quant-trading-workbench","market-news-collector","jq-quant","News-collector"):
    e={}
    rc,so,se=run("api",f"repos/jiaojingqi9-sudo/{name}",
                 "--jq",'{default_branch:.default_branch,size:.size,pushed:.pushed_at,empty:.size==0}')
    e["meta"]=so or se[:100]
    rc,so,se=run("api",f"repos/jiaojingqi9-sudo/{name}/commits","--jq",
                 '[.[]|{sha:.sha[0:7],date:.commit.author.date[0:10],msg:.commit.message|split("\n")[0]}]|.[0:4]')
    if rc==0 and so: e["commits"]=so
    else: e["commits"]=se[:80]
    rc,so,se=run("api",f"repos/jiaojingqi9-sudo/{name}/contents","--jq",'[.[].name]|.[0:14]')
    if rc==0 and so: e["top_level"]=so
    out["repos"][name]=e
print(json.dumps(out,ensure_ascii=False,indent=2))
