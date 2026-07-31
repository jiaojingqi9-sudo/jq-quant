#!/usr/bin/env python3
"""order_range_probe - 找出历史委托查询的可用时间跨度上限。

现象：查 2026-04-01 至今（4个月）报 Connection closed，重试 6 次耗时 35 秒。
逐级缩小范围，定位是「跨度太大」还是「接口本身坏了」。
"""
import json, os, sys, time
from datetime import date, timedelta
from pathlib import Path
TRADE=Path.home()/"All here"/"trade"
sys.path.insert(0,str(TRADE/"src")); os.chdir(TRADE)
from taa_futu.config import load_settings
from taa_futu.futu_gateway import FutuPaperTrader
s=load_settings()
today=date.today()
tests=[("今天",0),("7天",7),("14天",14),("30天",30),("60天",60),("90天",90),("120天",120)]
out={"kind":"order_range_probe","results":[]}
with FutuPaperTrader(s) as t:
    acc=t.resolve_trade_account()
    for label,days in tests:
        st_=(today-timedelta(days=days)).isoformat()
        t0=time.time()
        try:
            df=t.get_order_history(acc,st_,today.isoformat())
            out["results"].append({"范围":label,"起":st_,"秒":round(time.time()-t0,1),
                                   "行数":len(df),"结果":"成功"})
        except Exception as e:
            out["results"].append({"范围":label,"起":st_,"秒":round(time.time()-t0,1),
                                   "结果":f"失败: {type(e).__name__}: {str(e)[:60]}"})
print(json.dumps(out,ensure_ascii=False,indent=2))
