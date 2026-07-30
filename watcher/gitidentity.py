#!/usr/bin/env python3
"""gitidentity - 设置 git 全局用户名与邮箱（提交署名用，不是密码）。"""
import json, subprocess, sys
def run(*a):
    p=subprocess.run(["git",*a],capture_output=True,text=True,timeout=20)
    return p.returncode,(p.stdout or "").strip(),(p.stderr or "").strip()
out={"kind":"gitidentity"}
run("config","--global","user.name","Jiao")
run("config","--global","user.email","jiaojingqi9@gmail.com")
run("config","--global","init.defaultBranch","main")
_,n,_=run("config","--global","user.name")
_,e,_=run("config","--global","user.email")
_,b,_=run("config","--global","init.defaultBranch")
out["user_name"]=n; out["user_email"]=e; out["default_branch"]=b
print(json.dumps(out,ensure_ascii=False,indent=2))
