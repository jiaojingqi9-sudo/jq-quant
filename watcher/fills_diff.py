#!/usr/bin/env python3
"""fills_diff - 用 order_id 精确对出「券商有、本地流水没有」的成交。

对账显示账本以为还持有 IBIT/META/MSTR 而券商已清零。券商侧买卖数量完全相等，
说明确实平掉了；本地流水的笔数比券商少几笔，且最后一笔时间比券商早十分钟。
最可能是漏记了最近几笔成交。

这里按 order_id 做差集，把漏掉的那几笔连同数量金额一起列出来，并核对
「漏掉的卖出量」是否正好等于对账报的持仓缺口。只读。
"""
import json
import sys
from pathlib import Path

TRADE = Path.home() / "All here" / "trade"
sys.path.insert(0, str(TRADE / "src"))


def main() -> int:
    out = {"kind": "fills_diff"}
    from taa_futu.stock_runtime import STOCK_FILLS_FILE, load_stock_fill_records

    records = load_stock_fill_records(STOCK_FILLS_FILE)
    out["total_fills"] = len(records)
    if records:
        out["fill_record_keys"] = sorted(records[-1].keys())
        out["sample_last_fill"] = {k: str(v)[:40] for k, v in list(records[-1].items())[:14]}

    def rec_oid(r):
        for k in ("order_id", "orderId", "id"):
            if r.get(k) not in (None, ""):
                return str(r[k])
        return None

    local_ids = {rec_oid(r) for r in records}
    local_ids.discard(None)
    out["local_unique_order_ids"] = len(local_ids)

    try:
        from taa_futu.config import load_settings
        from taa_futu.futu_gateway import FutuPaperTrader
        settings = load_settings()
        with FutuPaperTrader(settings) as trader:
            acc = trader.resolve_trade_account()
            hist = trader.get_order_history(acc, "2026-04-01", "2026-07-31")
    except Exception as exc:
        out["broker_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 1

    filled = hist[hist["order_status"].astype(str).str.contains("FILLED", case=False, na=False)].copy()
    filled["dealt_qty"] = filled["dealt_qty"].astype(float)
    filled = filled[filled["dealt_qty"] > 0]
    out["broker_filled_orders"] = int(len(filled))

    filled["oid"] = filled["order_id"].astype(str)
    missing = filled[~filled["oid"].isin(local_ids)]
    out["missing_count"] = int(len(missing))

    rows = []
    for r in missing.sort_values("updated_time").to_dict("records"):
        rows.append({
            "order_id": str(r.get("order_id")),
            "code": r.get("code"),
            "side": r.get("trd_side"),
            "qty": round(float(r.get("dealt_qty") or 0), 2),
            "price": round(float(r.get("dealt_avg_price") or 0), 2),
            "amount": round(float(r.get("dealt_qty") or 0) * float(r.get("dealt_avg_price") or 0), 2),
            "updated": str(r.get("updated_time"))[:19],
        })
    out["missing_orders"] = rows[:40]

    # 漏掉的净数量，按标的汇总。卖出记负——正好应等于对账报的缺口
    net = {}
    amt = 0.0
    for r in rows:
        sign = -1 if "SELL" in str(r["side"]).upper() else 1
        net[r["code"]] = round(net.get(r["code"], 0.0) + sign * r["qty"], 2)
        amt += (-sign) * r["amount"]          # 卖出进现金
    out["missing_net_qty_by_symbol"] = net
    out["missing_cash_effect"] = round(amt, 2)

    # 本地与券商各自的最后一笔时间
    def last_ts(rs):
        vals = [str(r.get("ts") or r.get("updated_time") or "")[:19] for r in rs]
        vals = [v for v in vals if v]
        return max(vals) if vals else None
    out["local_last_fill"] = last_ts(records)
    out["broker_last_filled"] = str(filled["updated_time"].max())[:19]

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
