#!/usr/bin/env python3
"""一次性拉多只股票的轻量快照"""
import argparse, json, os, sys, traceback
from datetime import datetime

def main():
    p = argparse.ArgumentParser()
    p.add_argument("symbols")
    p.add_argument("--host", default=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.getenv("FUTU_OPEND_PORT", "11111")))
    p.add_argument("--time-range", type=int, default=0)
    p.add_argument("--language-id", type=int, default=0)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    from futu import OpenQuoteContext, RET_OK
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    ctx = OpenQuoteContext(host=args.host, port=args.port)
    out = {"query_ts_utc": datetime.now().isoformat(), "stocks": [], "errors": {}}
    try:
        # 单只拉, 失败不影响其他
        for sym in symbols:
            try:
                ret, data = ctx.get_market_snapshot([sym])
                if ret != RET_OK:
                    out["errors"][sym] = str(data)
                    continue
                recs = data.to_dict(orient="records")
                for s in recs:
                    out["stocks"].append({
                        "code": s.get("code"),
                        "name": s.get("name"),
                        "update_time": str(s.get("update_time")),
                        "last": s.get("last_price"),
                        "bid": s.get("bid_price"),
                        "ask": s.get("ask_price"),
                        "high": s.get("high_price"),
                        "low": s.get("low_price"),
                        "prev_close": s.get("prev_close_price"),
                        "volume": s.get("volume"),
                        "turnover": s.get("turnover"),
                        "amplitude": s.get("amplitude"),
                        "volume_ratio": s.get("volume_ratio"),
                        "highest52": s.get("highest52weeks_price"),
                        "lowest52": s.get("lowest52weeks_price"),
                        "pe_ttm": s.get("pe_ttm_ratio"),
                        "mkt_cap": s.get("total_market_val"),
                        "sec_status": s.get("sec_status"),
                    })
            except Exception as e:
                out["errors"][sym] = f"{type(e).__name__}: {e}"
        print(json.dumps(out, default=str, ensure_ascii=False))
        return 0
    finally:
        ctx.close()

if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception as e:
        print(json.dumps({"err": f"{type(e).__name__}: {e}", "tb": traceback.format_exc()}, ensure_ascii=False))
        sys.exit(1)
