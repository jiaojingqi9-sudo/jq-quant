#!/usr/bin/env python3
"""desktop_scan - 看桌面上有哪些与本系统相关的入口（只读）。"""
import json, os
from pathlib import Path
D=Path.home()/"Desktop"
out={"kind":"desktop_scan","desktop":str(D),"exists":D.exists()}
if D.exists():
    items=[]
    for p in sorted(D.iterdir()):
        if p.name.startswith("."): continue
        items.append({"name":p.name,"type":"目录" if p.is_dir() else "文件",
                      "is_app":p.suffix==".app","is_cmd":p.suffix==".command",
                      "size_kb":round(p.stat().st_size/1024) if p.is_file() else None})
    out["items"]=items
    out["count"]=len(items)
    out["related"]=[i["name"] for i in items
                    if any(k in i["name"] for k in ("交易","市场","新闻","富途","量化","Trading","JQ","Quant","控制"))
                    or i["is_cmd"] or i["is_app"]]
print(json.dumps(out,ensure_ascii=False,indent=2))
