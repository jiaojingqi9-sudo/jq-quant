#!/usr/bin/env python3
"""backfill_fills - 把券商有、本地流水没有的成交补记进账本。

不加 --apply 只列出会补哪些，不写任何东西。

为什么需要手工补：那 5 笔漏记发生在 2026-06-05 / 07-17 / 07-24 / 07-30，
早于新加的 7 天回溯窗口能覆盖的范围。代码改完只能防住以后，追不回过去。

怎么补：完全复用 auto_trader 记账时用的那套写入路径与费用模型，
event_id 也用同样的 `futu_fill:{order_id}:{累计成交量}` 格式，
所以补进去的记录和自动记的没有区别，重复运行也不会写两遍。
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

TRADE = Path.home() / "All here" / "trade"
sys.path.insert(0, str(TRADE / "src"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--start", default="2026-04-01")
    ap.add_argument("--end", default=None)
    args, _ = ap.parse_known_args()

    out = {"kind": "backfill_fills", "applied": args.apply}

    from datetime import UTC, datetime
    from taa_futu.config import load_settings
    # 用和 auto_trader 记账时完全一样的费用函数（auto_trader.py:552），
    # 否则补进去的记录费用口径和自动记的不一致，账本会出现两套算法的混合。
    from taa_futu.costs import build_trade_cost_model, estimate_trade_cost
    from taa_futu.futu_gateway import FutuPaperTrader
    from taa_futu.stock_runtime import (STOCK_FILLS_FILE, append_stock_fill,
                                        load_recorded_stock_fill_ids,
                                        load_stock_fill_records)

    settings = load_settings()
    end = args.end or datetime.now(UTC).date().isoformat()

    before = load_stock_fill_records(STOCK_FILLS_FILE)
    out["fills_before"] = len(before)

    # 去重按 order_id，不按 event_id。
    #
    # event_id 是 `futu_fill:{order_id}:{累计成交量}`，而累计量是逐笔累加出来的
    # ——同一个委托分多次成交时，本地记的是各次的累计快照，未必等于券商最终的
    # dealt_qty。用 event_id 比对会把 3932 笔已记录的成交误判成"缺失"。
    # order_id 是券商侧的唯一键，只要这个委托本地记过任何一笔，就不该再补。
    # 这个口径与对账缺口交叉验证过：差集正好 5 笔，且净数量与三个持仓缺口完全吻合。
    known_order_ids = set()
    for rec in before:
        oid = str(rec.get("order_id") or "").strip()
        if oid:
            known_order_ids.add(oid)
    out["known_order_ids"] = len(known_order_ids)

    with FutuPaperTrader(settings) as trader:
        acc_id = trader.resolve_trade_account()
        history = trader.get_order_history(acc_id, args.start, end)
    out["broker_rows"] = int(len(history))

    filled = history[history["order_status"].astype(str)
                     .str.contains("FILLED", case=False, na=False)].copy()
    filled["dealt_qty"] = filled["dealt_qty"].astype(float)
    filled["dealt_avg_price"] = filled["dealt_avg_price"].astype(float)
    filled = filled[(filled["dealt_qty"] > 0) & (filled["dealt_avg_price"] > 0)]

    model = build_trade_cost_model(settings)
    todo = []
    for row in filled.sort_values("updated_time").to_dict("records"):
        order_id = str(row.get("order_id") or "").strip()
        qty = float(row.get("dealt_qty") or 0)
        price = float(row.get("dealt_avg_price") or 0)
        event_id = f"futu_fill:{order_id}:{qty:.8f}"
        if not order_id or order_id in known_order_ids:
            continue
        side = "SELL" if "SELL" in str(row.get("trd_side") or "").upper() else "BUY"
        symbol = str(row.get("code") or "").strip()
        ts = str(row.get("updated_time"))[:19]
        breakdown = estimate_trade_cost(side, qty, price, timestamp=ts, model=model)
        todo.append({
            "ts": ts,
            "symbol": symbol,
            "side": side,
            "quantity": qty,
            "price": price,
            "fee": round(float(getattr(breakdown, "total", 0.0)), 5),
            "fee_source": getattr(breakdown, "source", "estimated"),
            "event_id": event_id,
            "order_id": order_id,
            "cumulative_quantity": qty,
            "cumulative_notional": round(qty * price, 2),
            "strategy": "backfill",
            "source": "manual_backfill_20260731",
        })

    out["missing_count"] = len(todo)
    out["missing"] = [{k: v for k, v in r.items()
                       if k in ("ts", "symbol", "side", "quantity", "price", "fee", "order_id")}
                      for r in todo]

    if not args.apply:
        out["note"] = "这是计划，没有写入。加 --apply 才补记。"
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0

    if not todo:
        out["result"] = "没有需要补的成交"
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0

    backup = STOCK_FILLS_FILE.with_name(
        f"stock_fills.backup_before_backfill_{time.strftime('%Y%m%d_%H%M%S')}.jsonl")
    shutil.copy2(STOCK_FILLS_FILE, backup)
    out["backup"] = backup.name

    for record in todo:
        append_stock_fill(record, fills_path=STOCK_FILLS_FILE)

    after = load_stock_fill_records(STOCK_FILLS_FILE)
    out["fills_after"] = len(after)
    out["added"] = len(after) - len(before)

    # 补完再对一次账，看缺口是否消失
    try:
        from taa_futu.stock_ledger import (build_stock_double_entry_ledger,
                                           reconcile_stock_ledger)
        from taa_futu.stock_runtime import (STOCK_LEDGER_EPOCH_FILE,
                                            load_stock_ledger_epoch)
        journal = build_stock_double_entry_ledger(STOCK_FILLS_FILE,
                                                  epoch_path=STOCK_LEDGER_EPOCH_FILE)
        epoch = load_stock_ledger_epoch()
        with FutuPaperTrader(settings) as trader:
            acc_id = trader.resolve_trade_account()
            account = trader.get_account_info(acc_id)
            positions = trader.get_positions(acc_id)
        recon = reconcile_stock_ledger(journal, positions=positions,
                                       account=account, epoch=epoch)
        out["after_reconciliation"] = {
            "ok": bool(getattr(recon, "ok", False)),
            "break_count": len(getattr(recon, "breaks", []) or []),
            "breaks": [{"kind": b.kind, "symbol": b.symbol,
                        "expected": round(float(b.expected), 2),
                        "actual": round(float(b.actual), 2),
                        "diff": round(float(b.difference), 2)}
                       for b in (getattr(recon, "breaks", []) or [])[:8]],
            "net_realized": round(float(getattr(journal, "net_realized_pnl", 0.0)), 2),
            "entries": len(getattr(journal, "entries", []) or []),
            "chain_valid": bool(getattr(journal, "chain_valid", False)),
        }
    except Exception as exc:
        out["recheck_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
