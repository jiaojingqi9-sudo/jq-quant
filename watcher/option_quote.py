#!/usr/bin/env python3
"""拉某只票最近几个月份期权链 + 关键行权价的实时 bid/ask/Greeks"""
import argparse, json, os, sys, traceback
from datetime import datetime, timedelta

def main():
    p = argparse.ArgumentParser()
    p.add_argument("stock_symbol", help="如 US.NVDA / US.GOOGL")
    p.add_argument("--host", default=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.getenv("FUTU_OPEND_PORT", "11111")))
    p.add_argument("--strike-range", type=float, default=15.0,
                   help="围绕现价 ±X%% 抓行权价, 默认 15%%")
    p.add_argument("--expiries", type=int, default=2,
                   help="抓最近几个到期月份, 默认 2")
    p.add_argument("--option-side", default="CALL", choices=["CALL","PUT","BOTH"])
    p.add_argument("--time-range", type=int, default=0)
    p.add_argument("--language-id", type=int, default=0)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    from futu import OpenQuoteContext, RET_OK, OptionType
    ctx = OpenQuoteContext(host=args.host, port=args.port)
    out = {"query_ts_utc": datetime.utcnow().isoformat(), "symbol": args.stock_symbol}
    try:
        # 1. 正股
        ret, data = ctx.get_market_snapshot([args.stock_symbol])
        if ret == RET_OK:
            recs = data.to_dict(orient="records")
            s = recs[0]
            spot = s.get("last_price")
            out["stock"] = {
                "code": s["code"], "update_time_et": str(s["update_time"]),
                "last_price": spot, "bid": s.get("bid_price"), "ask": s.get("ask_price"),
                "high": s["high_price"], "low": s["low_price"],
                "prev_close": s.get("prev_close_price"),
                "volume": s["volume"], "sec_status": s.get("sec_status"),
            }
        else:
            out["stock_err"] = str(data); print(json.dumps(out, default=str, ensure_ascii=False)); return 1

        # 2. 期权到期日
        ret, data = ctx.get_option_expiration_date(args.stock_symbol)
        if ret != RET_OK:
            out["expiry_err"] = str(data); print(json.dumps(out, default=str, ensure_ascii=False)); return 1
        exps = data.to_dict(orient="records") if hasattr(data, "to_dict") else data
        today = datetime.now().date()
        future_exps = []
        for e in exps:
            ed = e.get("strike_time") or e.get("strike_date")
            if not ed: continue
            try:
                d = datetime.strptime(str(ed)[:10], "%Y-%m-%d").date()
                if d >= today:
                    future_exps.append((d, str(ed)[:10]))
            except: pass
        future_exps.sort()
        # 跳过 22 天内的短期/周期权,优先抓月期权
        min_days = (datetime.now().date() - datetime.now().date()).days  # placeholder
        far = [(d, s) for d, s in future_exps if (d - datetime.now().date()).days >= 22]
        target_exps = far[:args.expiries] if far else future_exps[:args.expiries]
        out["target_expiries"] = [te[1] for te in target_exps]

        # 3. 每个到期日抓 chain
        lo = spot * (1 - args.strike_range/100)
        hi = spot * (1 + args.strike_range/100)
        sides = [OptionType.CALL] if args.option_side=="CALL" else \
                [OptionType.PUT]  if args.option_side=="PUT"  else \
                [OptionType.CALL, OptionType.PUT]

        results = {}
        for expd, exp_str in target_exps:
            results[exp_str] = {}
            for side in sides:
                ret, data = ctx.get_option_chain(
                    code=args.stock_symbol,
                    start=exp_str, end=exp_str,
                    option_type=side,
                )
                if ret != RET_OK:
                    results[exp_str][str(side)+"_err"] = str(data); continue
                chain = data.to_dict(orient="records")
                # 过滤到目标行权价区间
                targets = [r for r in chain
                           if r.get("strike_price") is not None
                           and lo <= r["strike_price"] <= hi]
                if not targets:
                    results[exp_str][str(side)] = []
                    continue
                codes = [r["code"] for r in targets if r.get("code")]
                # 批量拉报价
                ret2, qdata = ctx.get_market_snapshot(codes)
                quotes = []
                if ret2 == RET_OK:
                    qrecs = qdata.to_dict(orient="records")
                    for q in qrecs:
                        quotes.append({
                            "strike": q.get("option_strike_price"),
                            "last": q.get("last_price"),
                            "bid": q.get("bid_price"),
                            "ask": q.get("ask_price"),
                            "volume": q.get("volume"),
                            "oi": q.get("option_open_interest"),
                            "iv": q.get("option_implied_volatility"),
                            "delta": q.get("option_delta"),
                            "theta": q.get("option_theta"),
                            "gamma": q.get("option_gamma"),
                        })
                    quotes.sort(key=lambda x: x["strike"] or 0)
                results[exp_str][str(side)] = quotes

        out["option_chains"] = results
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
