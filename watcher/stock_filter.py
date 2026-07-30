#!/usr/bin/env python3
"""市场级条件选股: 封装 futu get_stock_filter。MULTI 模式, 读 --task-json。带限频保护。"""
import argparse, json, os, sys, time, traceback
FIELD_MAP = {
 "cur_price":"CUR_PRICE","price_to_52high":"CUR_PRICE_TO_HIGHEST52_WEEKS_RATIO",
 "price_to_52low":"CUR_PRICE_TO_LOWEST52_WEEKS_RATIO","volume_ratio":"VOLUME_RATIO",
 "market_val":"MARKET_VAL","turnover_rate":"TURNOVER_RATE",
}
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--task-json",required=True)
    ap.add_argument("--host",default=os.getenv("FUTU_OPEND_HOST","127.0.0.1"))
    ap.add_argument("--port",type=int,default=int(os.getenv("FUTU_OPEND_PORT","11111")))
    a=ap.parse_args(); task=json.loads(a.task_json)
    from futu import OpenQuoteContext, RET_OK, StockField, SortDir, Market
    from futu.quote.quote_stockfilter_info import SimpleFilter
    ctx=OpenQuoteContext(host=a.host,port=a.port)
    out={"market":task.get("market","US"),"filters":[],"count":0,"stocks":[],"error":None}
    try:
        if task.get("list_fields"):
            out["available_fields"]=[x for x in dir(StockField) if not x.startswith("_")]
            print(json.dumps(out,default=str,ensure_ascii=False)); return 0
        filters=[]
        for c in task.get("conditions",[]):
            f=SimpleFilter(); fld=FIELD_MAP.get(c["field"],str(c["field"]).upper())
            f.stock_field=getattr(StockField,fld)
            if c.get("min") is not None: f.filter_min=c["min"]
            if c.get("max") is not None: f.filter_max=c["max"]
            f.is_no_filter=(c.get("min") is None and c.get("max") is None)
            if c.get("sort"): f.sort=SortDir.DESCEND if c["sort"]=="desc" else SortDir.ASCEND
            filters.append(f); out["filters"].append({"field":fld,"min":c.get("min"),"max":c.get("max")})
        mk=getattr(Market,task.get("market","US")); cap=int(task.get("max",400))
        begin=0; collected=[]
        while begin<cap:
            ret=None
            for attempt in range(3):
                ret,data=ctx.get_stock_filter(market=mk,filter_list=filters,begin=begin,num=200)
                if ret==RET_OK: break
                if "频率" in str(data): time.sleep(31); continue
                break
            if ret!=RET_OK: out["error"]=str(data); break
            if isinstance(data,tuple) and len(data)==3:
                last_page,all_count,ls=data; out["all_count"]=all_count
            else: ls=data; last_page=True
            for it in ls:
                rec={"code":getattr(it,"stock_code",None),"name":getattr(it,"stock_name",None)}
                for k,v in getattr(it,"__dict__",{}).items():
                    if k not in ("stock_code","stock_name") and isinstance(v,(int,float,str,bool)): rec[k]=v
                collected.append(rec)
            begin+=200
            if last_page or len(ls)<200: break
            time.sleep(3.3)
        out["count"]=len(collected); out["stocks"]=collected
        print(json.dumps(out,default=str,ensure_ascii=False)); return 0
    except Exception as e:
        out["error"]=f"{type(e).__name__}: {e}"; out["tb"]=traceback.format_exc()
        print(json.dumps(out,default=str,ensure_ascii=False)); return 1
    finally:
        ctx.close()
if __name__=="__main__":
    sys.exit(main() or 0)
