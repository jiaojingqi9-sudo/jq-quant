#!/usr/bin/env python3
"""一次性诊断：拉 5/21 NVDA 1分K，找当天最高点的发生时间"""
import argparse
import json
import sys
import traceback

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--time-range", type=int, default=0)
    p.add_argument("--language-id", type=int, default=0)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    from futu import OpenQuoteContext, RET_OK, KLType, AuType
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    out = {}
    try:
        ret, data, _ = ctx.request_history_kline(
            "US.NVDA", start="2026-05-21", end="2026-05-21",
            ktype=KLType.K_1M, autype=AuType.QFQ,
            max_count=500,
        )
        if ret != RET_OK:
            out["err"] = str(data)
            print(json.dumps(out))
            return 1
        recs = data.to_dict(orient="records") if hasattr(data, "to_dict") else data
        if not recs:
            out["err"] = "no bars"
            print(json.dumps(out, default=str))
            return 1

        high_bar = max(recs, key=lambda r: r["high"])
        low_bar = min(recs, key=lambda r: r["low"])
        out["high_time"] = str(high_bar["time_key"])
        out["high_price"] = high_bar["high"]
        out["low_time"] = str(low_bar["time_key"])
        out["low_price"] = low_bar["low"]
        out["bar_count"] = len(recs)
        out["first_bar"] = str(recs[0]["time_key"])
        out["last_bar"] = str(recs[-1]["time_key"])

        out["around_13_22"] = [
            {"t": str(r["time_key"]), "o": r["open"], "h": r["high"],
             "l": r["low"], "c": r["close"], "v": r.get("volume")}
            for r in recs if str(r["time_key"]).startswith("2026-05-21 13:2")
        ][:6]

        before, after = [], []
        for r in recs:
            t = str(r["time_key"])
            hm = t[11:16] if len(t) >= 16 else ""
            if hm and hm < "13:22":
                before.append(r)
            elif hm:
                after.append(r)
        if before:
            hb = max(before, key=lambda r: r["high"])
            lb = min(before, key=lambda r: r["low"])
            out["before_1322"] = {
                "bars": len(before),
                "high": hb["high"], "high_time": str(hb["time_key"]),
                "low": lb["low"], "low_time": str(lb["time_key"]),
            }
        if after:
            ha = max(after, key=lambda r: r["high"])
            la = min(after, key=lambda r: r["low"])
            out["after_1322"] = {
                "bars": len(after),
                "high": ha["high"], "high_time": str(ha["time_key"]),
                "low": la["low"], "low_time": str(la["time_key"]),
            }

        print(json.dumps(out, default=str, ensure_ascii=False))
        return 0
    finally:
        ctx.close()


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception as e:
        print(json.dumps({"err": f"{type(e).__name__}: {e}",
                          "tb": traceback.format_exc()}, ensure_ascii=False))
        sys.exit(1)
