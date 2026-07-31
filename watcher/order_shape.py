#!/usr/bin/env python3
"""order_shape - 看历史委托的列结构与规模，为缓存设计提供依据。"""
import json, sys
from pathlib import Path
TRADE=Path.home()/"All here"/"trade"
sys.path.insert(0,str(TRADE/"src"))
import os; os.chdir(TRADE)
from taa_futu.config import load_settings
from taa_futu.futu_gateway import FutuPaperTrader
s=load_settings()
out={"kind":"order_shape"}
with FutuPaperTrader(s) as t:
    acc=t.resolve_trade_account()
    import time
    t0=time.time()
    df=t.get_order_history(acc,"2026-04-01","2026-07-31")
    out["fetch_sec"]=round(time.time()-t0,1)
    out["rows"]=len(df); out["cols"]=list(df.columns)
    if not df.empty:
        out["sample"]={k:str(v)[:40] for k,v in df.iloc[0].to_dict().items()}
        for c in ("order_id","create_time","updated_time"):
            if c in df.columns:
                out[f"{c}_unique"]=int(df[c].nunique())
print(json.dumps(out,ensure_ascii=False,indent=2))
