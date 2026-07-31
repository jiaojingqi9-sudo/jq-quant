#!/usr/bin/env python3
"""break_probe - 查清对账差异的来源：本地成交流水里到底有没有那三只票的卖出。

对账说账本认为还持有 IBIT/META/MSTR，而券商已清零，同时现金多 7.7 万。
两种可能：
  a) 卖出确实发生了，但成交没写进 stock_fills.jsonl（漏记）
  b) 卖出写进去了，但账本投影算错了（代码 bug）

分辨方法：直接数流水里这三只票的买入卖出，和券商历史委托对照。
只读。
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

TRADE = Path.home() / "All here" / "trade"
sys.path.insert(0, str(TRADE / "src"))

SYMBOLS = ["US.IBIT", "US.META", "US.MSTR"]


def main() -> int:
    out = {"kind": "break_probe"}
    from taa_futu.stock_runtime import STOCK_FILLS_FILE, load_stock_fill_records

    records = load_stock_fill_records(STOCK_FILLS_FILE)
    out["total_fills"] = len(records)

    local = defaultdict(lambda: {"buy_qty": 0.0, "sell_qty": 0.0,
                                 "buy_amt": 0.0, "sell_amt": 0.0,
                                 "n_buy": 0, "n_sell": 0, "last": None})
    for r in records:
        code = r.get("code") or r.get("symbol")
        if code not in SYMBOLS:
            continue
        side = str(r.get("side") or r.get("trd_side") or "").upper()
        qty = float(r.get("qty") or r.get("dealt_qty") or 0)
        px = float(r.get("price") or r.get("dealt_avg_price") or 0)
        bucket = local[code]
        if "SELL" in side:
            bucket["sell_qty"] += qty
            bucket["sell_amt"] += qty * px
            bucket["n_sell"] += 1
        elif "BUY" in side:
            bucket["buy_qty"] += qty
            bucket["buy_amt"] += qty * px
            bucket["n_buy"] += 1
        ts = r.get("ts") or r.get("updated_time") or r.get("create_time")
        if ts:
            bucket["last"] = str(ts)[:19]
    out["local_fills"] = {k: {kk: (round(vv, 2) if isinstance(vv, float) else vv)
                              for kk, vv in v.items()} for k, v in local.items()}
    out["local_net_qty"] = {k: round(v["buy_qty"] - v["sell_qty"], 2)
                            for k, v in local.items()}

    # 券商那边的历史委托怎么说
    try:
        from taa_futu.config import load_settings
        from taa_futu.futu_gateway import FutuPaperTrader
        settings = load_settings()
        with FutuPaperTrader(settings) as trader:
            acc = trader.resolve_trade_account()
            hist = trader.get_order_history(acc, "2026-04-01", "2026-07-31")
        out["broker_history_rows"] = int(len(hist))
        if len(hist):
            sub = hist[hist["code"].isin(SYMBOLS)]
            out["broker_rows_for_symbols"] = int(len(sub))
            filled = sub[sub["order_status"].astype(str).str.contains("FILLED", case=False, na=False)]
            summary = {}
            for code, grp in filled.groupby("code"):
                buys = grp[grp["trd_side"].astype(str).str.upper().str.contains("BUY")]
                sells = grp[grp["trd_side"].astype(str).str.upper().str.contains("SELL")]
                summary[code] = {
                    "buy_qty": round(float(buys["dealt_qty"].astype(float).sum()), 2),
                    "sell_qty": round(float(sells["dealt_qty"].astype(float).sum()), 2),
                    "sell_amount": round(float((sells["dealt_qty"].astype(float)
                                                * sells["dealt_avg_price"].astype(float)).sum()), 2),
                    "n_orders": int(len(grp)),
                    "last_update": str(grp["updated_time"].max())[:19],
                }
            out["broker_filled"] = summary
            out["broker_net_qty"] = {k: round(v["buy_qty"] - v["sell_qty"], 2)
                                     for k, v in summary.items()}
            out["broker_total_sell_amount"] = round(
                sum(v["sell_amount"] for v in summary.values()), 2)
    except Exception as exc:
        out["broker_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"

    # 流水覆盖的时间范围，看是不是某个时间点之后就断了
    stamps = [str(r.get("ts") or r.get("updated_time") or "")[:19]
              for r in records if (r.get("ts") or r.get("updated_time"))]
    stamps = sorted(s for s in stamps if s)
    if stamps:
        out["fills_time_range"] = {"first": stamps[0], "last": stamps[-1]}

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
