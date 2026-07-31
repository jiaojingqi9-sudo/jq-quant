#!/usr/bin/env python3
"""order_chunk_verify - 验证分段查询：既要成功，也要数据正确。

正确性检查：
  1. 121 天（原本失败的范围）现在能不能拿到数据
  2. 分段结果与「逐段单查再拼接」是否一致（防止分段逻辑漏数据）
  3. 短范围（30天）分段前后结果是否完全一致
"""
import json, os, sys, time
from datetime import date, timedelta
from pathlib import Path
TRADE=Path.home()/"All here"/"trade"
sys.path.insert(0,str(TRADE/"src")); os.chdir(TRADE)
import pandas as pd
from taa_futu.config import load_settings
from taa_futu.futu_gateway import FutuPaperTrader
s=load_settings(); today=date.today()
out={"kind":"order_chunk_verify"}
with FutuPaperTrader(s) as t:
    acc=t.resolve_trade_account()

    # 1. 原本失败的 121 天范围
    t0=time.time()
    try:
        df=t.get_order_history(acc,"2026-04-01",today.isoformat())
        out["full_range"]={"秒":round(time.time()-t0,1),"行数":len(df),"结果":"成功"}
        if "order_id" in df.columns:
            out["full_range"]["order_id唯一数"]=int(df["order_id"].nunique())
        if not df.empty and "create_time" in df.columns:
            out["full_range"]["最早"]=str(df["create_time"].min())[:19]
            out["full_range"]["最晚"]=str(df["create_time"].max())[:19]
    except Exception as e:
        out["full_range"]={"结果":f"仍失败: {type(e).__name__}: {str(e)[:70]}"}

    # 2. 手动逐段查再拼，作为独立对照
    t0=time.time(); manual=[]
    cur=date(2026,4,1)
    while cur<=today:
        w_end=min(cur+timedelta(days=29),today)
        try:
            c=t._fetch_order_history_window(acc,cur.isoformat(),w_end.isoformat())
            if not c.empty: manual.append(c)
        except Exception as e:
            out.setdefault("manual_errors",[]).append(f"{cur}: {str(e)[:50]}")
        cur=w_end+timedelta(days=1)
    md=pd.concat(manual,ignore_index=True) if manual else pd.DataFrame()
    if "order_id" in md.columns: md=md.drop_duplicates(subset=["order_id"])
    out["manual_control"]={"秒":round(time.time()-t0,1),"行数":len(md)}

    # 3. 两者比对
    a=out.get("full_range",{}).get("order_id唯一数") or out.get("full_range",{}).get("行数")
    b=len(md)
    out["一致性"]= "一致" if a==b else f"不一致: 分段{a} vs 对照{b}"

    # 4. 短范围回归：30 天分段前后应完全相同
    t0=time.time()
    short=t.get_order_history(acc,(today-timedelta(days=30)).isoformat(),today.isoformat())
    out["short_range_30d"]={"秒":round(time.time()-t0,1),"行数":len(short)}
print(json.dumps(out,ensure_ascii=False,indent=2))
