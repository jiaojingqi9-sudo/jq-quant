#!/usr/bin/env python3
"""restart_news_stack - 重启所有会生成看板的新闻线进程。

坑：不止 collect 生成看板，review-api 也会（review_api.py 调
refresh_runtime_status_views）。只重启 collect 的话，跑着旧代码的 review-api
会把看板覆盖回旧样式——表现为「源码明明改了，界面就是不变」。
"""
import json, os, subprocess, sys, time
LABELS=["ai.codex.marketnews.collect","ai.codex.marketnews.reviewapi",
        "ai.codex.marketnews.notify","ai.codex.marketnews.newslearning"]
PATTERNS=["market_news collect","market_news review-api","market_news notify",
          "market_news news-learning"]
def run(cmd,t=30):
    p=subprocess.run(cmd,capture_output=True,text=True,timeout=t)
    return p.returncode,(p.stdout or "").strip(),(p.stderr or "").strip()
def main():
    uid=os.getuid(); out={"kind":"restart_news_stack","actions":[]}
    rc,so,_=run(["ps","-Ao","pid,command"])
    killed=[]
    for pat in PATTERNS:
        for line in so.splitlines():
            if pat in line and "grep" not in line:
                pid=line.strip().split()[0]
                run(["kill","-9",pid]); killed.append((pat,pid))
    out["killed"]=killed
    time.sleep(2)
    for lb in LABELS:
        rc,_,se=run(["launchctl","kickstart","-k",f"gui/{uid}/{lb}"])
        out["actions"].append({"label":lb,"rc":rc,"err":se[:80]})
    time.sleep(8)
    rc,so,_=run(["ps","-Ao","pid,etime,command"])
    out["running_after"]=[l.strip()[:95] for l in so.splitlines()
                          if "market_news" in l and "grep" not in l]
    print(json.dumps(out,ensure_ascii=False,indent=2)); return 0
sys.exit(main())
