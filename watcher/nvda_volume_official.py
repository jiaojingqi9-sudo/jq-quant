#!/usr/bin/env python3
"""从富途官方接口取 NVDA 的日线成交量，用来核对本地标定用的口径。

为什么要这一步：练习台的成交量原本是从本地 klines.jsonl 累加出来的，
但同一份数据里 snapshots.jsonl 报的当日累计成交量比它高 45%。
两个都是「自己攒的」，谁对谁错分不清，必须拿接口的官方日线来断。

日线的 volume 字段是交易所口径的当日总成交量，是最权威的那个数。

用法（邮差）：
    {"skill": "nvda_volume_official"}
"""
import json
import sys
import traceback


def main():
    out = {"kind": "nvda_volume_official"}
    try:
        from futu import OpenQuoteContext, KLType, AuType, SubType
    except Exception as exc:
        print(json.dumps({"error": f"import futu 失败: {exc}"}, ensure_ascii=False))
        return 1

    host = "127.0.0.1"
    port = 11111
    ctx = None
    try:
        ctx = OpenQuoteContext(host=host, port=port)

        # 1) 日线：官方口径的每日成交量与成交额
        # request_history_kline 返回三个值：(ret, dataframe, page_req_key)
        res = ctx.request_history_kline(
            "US.NVDA", start="2026-07-20", end="2026-08-05",
            ktype=KLType.K_DAY, autype=AuType.QFQ,
            max_count=60)
        ret, df = res[0], res[1]
        if ret != 0:
            out["kline_error"] = str(df)
        else:
            rows = df.to_dict(orient="records") if hasattr(df, "to_dict") else []
            out["daily"] = [
                {"date": str(r.get("time_key"))[:10],
                 "close": r.get("close"),
                 "volume": r.get("volume"),
                 "turnover": r.get("turnover")}
                for r in rows
            ]

        # 2) 分时段日线：美股支持 RTH / ETH / ALL，用来拆出盘中与盘前盘后
        for label, sess in (("RTH", "RTH"), ("ALL", "ALL")):
            try:
                res2 = ctx.request_history_kline(
                    "US.NVDA", start="2026-08-03", end="2026-08-05",
                    ktype=KLType.K_DAY, autype=AuType.QFQ,
                    max_count=10, session=sess)
                ret2, df2 = res2[0], res2[1]
                if ret2 == 0 and hasattr(df2, "to_dict"):
                    out[f"daily_{label}"] = [
                        {"date": str(r.get("time_key"))[:10], "volume": r.get("volume"),
                         "turnover": r.get("turnover")}
                        for r in df2.to_dict(orient="records")]
                else:
                    out[f"daily_{label}_error"] = str(df2)[:200]
            except TypeError as exc:
                out[f"daily_{label}_error"] = f"这个 SDK 版本不支持 session 参数: {exc}"
            except Exception as exc:
                out[f"daily_{label}_error"] = str(exc)[:200]

        # 3) 快照里的 volume 字段，跟本地 snapshots.jsonl 同源，用来对齐
        ret3, snap = ctx.get_market_snapshot(["US.NVDA"])
        if ret3 == 0 and hasattr(snap, "to_dict"):
            r = snap.to_dict(orient="records")[0]
            out["snapshot"] = {k: r.get(k) for k in
                               ("update_time", "last_price", "volume", "turnover",
                                "turnover_rate", "prev_close_price")}
        else:
            out["snapshot_error"] = str(snap)[:200]

        # 4) 摆盘：看看返回的档位里有没有碎股，以及有没有交易所标识
        # 实时摆盘要先订阅
        rs, rm = ctx.subscribe(["US.NVDA"], [SubType.ORDER_BOOK])
        out["subscribe"] = "ok" if rs == 0 else str(rm)[:200]
        import time as _t
        _t.sleep(2)
        ret4, ob = ctx.get_order_book("US.NVDA", num=10)
        if ret4 == 0 and isinstance(ob, dict):
            out["orderbook_keys"] = sorted(ob.keys())
            out["orderbook_bid_top5"] = ob.get("Bid", [])[:5]
            out["orderbook_ask_top5"] = ob.get("Ask", [])[:5]
        else:
            out["orderbook_error"] = str(ob)[:300]

    except Exception as exc:
        out["error"] = f"{exc}\n{traceback.format_exc()[-800:]}"
    finally:
        try:
            if ctx is not None:
                ctx.close()
        except Exception:
            pass

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
