"""Pull the authoritative broker order history from Futu OpenD (READ-ONLY).

Run on the Mac that has OpenD running + logged in:

    cd <repo root>
    .venv/bin/python stock/tools/pull_broker_history.py

It queries history_order_list_query month-by-month (Jan→now), keeps only filled
orders (dealt_qty > 0), computes FIFO realized P&L per symbol with the project
cost model, and writes:

    runtime/broker_history_raw.json      (every filled order — basis for the repair)
    runtime/broker_history_summary.json  (per-month + per-symbol + totals)

It writes NOTHING to the ledger or fills. It only reads from the broker.
"""

from __future__ import annotations

import json
from collections import deque, defaultdict
from datetime import date, timedelta

from taa_futu.config import load_settings
from taa_futu.costs import build_trade_cost_model, estimate_trade_cost
from taa_futu.futu_gateway import FutuPaperTrader


def _side(raw) -> str:
    s = str(raw).upper()
    if "BUY" in s:
        return "BUY"
    if "SELL" in s:
        return "SELL"
    return s


def _months(start_year=2026, start_month=1):
    """Yield (first_day, last_day) ISO strings for each month from the start
    up to and including the current month."""
    today = date.today()
    out = []
    y, m = start_year, start_month
    while (y, m) <= (today.year, today.month):
        first = date(y, m, 1)
        nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        last = nxt - timedelta(days=1)
        out.append((first.isoformat(), last.isoformat()))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def main() -> int:
    settings = load_settings()
    cost_model = build_trade_cost_model(settings)

    filled: list[dict] = []
    seen_ids: set[str] = set()

    with FutuPaperTrader(settings) as trader:
        acc_id = trader.resolve_trade_account()
        print(f"account: {acc_id}  env: {settings.futu_trd_env}")
        account = trader.get_account_info(acc_id)
        try:
            total_assets = float(account.get("total_assets", 0) or 0)
            cash = float(account.get("cash", account.get("cash_balance", 0)) or 0)
        except Exception:
            total_assets = cash = 0.0
        print(f"broker total_assets={total_assets:,.2f}  cash={cash:,.2f}")

        for start, end in _months():
            try:
                hist = trader.get_order_history(acc_id, start, end)
            except Exception as exc:  # noqa: BLE001
                print(f"  {start}..{end}: query failed ({type(exc).__name__}: {exc})")
                continue
            n_month = 0
            if hist is not None and not hist.empty:
                for _, row in hist.iterrows():
                    oid = str(row.get("order_id", ""))
                    dealt = float(row.get("dealt_qty", 0) or 0)
                    price = float(row.get("dealt_avg_price", 0) or 0)
                    if dealt <= 0 or price <= 0 or oid in seen_ids:
                        continue
                    seen_ids.add(oid)
                    filled.append({
                        "ts": str(row.get("create_time", "")),
                        "code": str(row.get("code", "")),
                        "side": _side(row.get("trd_side", "")),
                        "qty": dealt,
                        "price": price,
                        "order_id": oid,
                        "status": str(row.get("order_status", "")),
                    })
                    n_month += 1
            print(f"  {start[:7]}: {n_month} filled order(s)")

    filled.sort(key=lambda r: r["ts"])
    # FIFO realized P&L per symbol with cost model
    lots: dict[str, deque] = defaultdict(deque)
    per_symbol = defaultdict(lambda: {"gross": 0.0, "fees": 0.0, "buys": 0, "sells": 0, "unmatched": 0})
    per_month = defaultdict(lambda: {"orders": 0, "buys": 0, "sells": 0, "notional": 0.0})
    for f in filled:
        sym, side, qty, price = f["code"], f["side"], f["qty"], f["price"]
        ts = estimate_trade_cost(side, qty, price, timestamp=None, model=cost_model)
        fee = float(getattr(ts, "total", 0.0) or 0.0)
        mo = f["ts"][:7]
        pm = per_month[mo]; pm["orders"] += 1; pm["notional"] += qty * price
        a = per_symbol[sym]; a["fees"] += fee
        if side == "BUY":
            a["buys"] += 1; pm["buys"] += 1
            lots[sym].append([qty, price])
        elif side == "SELL":
            a["sells"] += 1; pm["sells"] += 1
            rem = qty
            while rem > 1e-9 and lots[sym]:
                lot = lots[sym][0]; m = min(rem, lot[0])
                a["gross"] += m * (price - lot[1]); rem -= m; lot[0] -= m
                if lot[0] <= 1e-9:
                    lots[sym].popleft()
            if rem > 1e-9:
                a["unmatched"] += 1

    tot_gross = sum(a["gross"] for a in per_symbol.values())
    tot_fees = sum(a["fees"] for a in per_symbol.values())
    summary = {
        "generated_at": date.today().isoformat(),
        "broker_total_assets": total_assets,
        "broker_cash": cash,
        "filled_order_count": len(filled),
        "date_range": [filled[0]["ts"][:10], filled[-1]["ts"][:10]] if filled else [],
        "total_realized_gross": round(tot_gross, 2),
        "total_fees": round(tot_fees, 2),
        "total_realized_net": round(tot_gross - tot_fees, 2),
        "per_month": {k: {**v, "notional": round(v["notional"], 2)} for k, v in sorted(per_month.items())},
        "per_symbol": {k: {kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()}
                       for k, v in sorted(per_symbol.items(), key=lambda x: -(x[1]["gross"] - x[1]["fees"]))},
    }

    import os
    os.makedirs("runtime", exist_ok=True)
    json.dump(filled, open("runtime/broker_history_raw.json", "w"), indent=2)
    json.dump(summary, open("runtime/broker_history_summary.json", "w"), ensure_ascii=False, indent=2)

    print("\n=== 券商成交历史（权威，只读）===")
    print(f"filled orders: {len(filled)}   range: {summary['date_range']}")
    print(f"\n{'月份':<10}{'订单':>7}{'买':>6}{'卖':>6}{'成交额$':>16}")
    for mo, v in summary["per_month"].items():
        print(f"{mo:<10}{v['orders']:>7}{v['buys']:>6}{v['sells']:>6}{v['notional']:>16,.0f}")
    print(f"\n已实现毛利 ${tot_gross:,.0f} − 费用 ${tot_fees:,.0f} = 净利 ${tot_gross - tot_fees:,.0f} (估算费用)")
    print("\n写出: runtime/broker_history_raw.json, runtime/broker_history_summary.json")
    print("（Claude 能直接读这两个文件，跑完说一声即可。）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
