"""READ-ONLY snapshot of the REAL (live-money) Futu account.

Strictly read-only. It only calls query endpoints:
    get_acc_list, accinfo_query, position_list_query, history_order_list_query
It NEVER places, modifies, or cancels an order, never unlocks trading, and never
moves money. There is no order/unlock code in this file at all.

Run on the Mac with OpenD running + logged into your real account:

    cd <repo root>
    .venv/bin/python stock/tools/pull_real_account.py

Writes runtime/real_account_snapshot.json (Claude can read it directly).
"""

from __future__ import annotations

import json
from collections import deque, defaultdict
from datetime import date, timedelta

from taa_futu.config import load_settings
from taa_futu.costs import build_trade_cost_model, estimate_trade_cost


def _side(raw) -> str:
    s = str(raw).upper()
    return "BUY" if "BUY" in s else ("SELL" if "SELL" in s else s)


def main() -> int:
    settings = load_settings()

    # Fail fast if OpenD is not listening, instead of the SDK's endless retry loop.
    import socket
    try:
        with socket.create_connection((settings.futu_host, settings.futu_port), timeout=3):
            pass
    except OSError:
        print(f"连不上 OpenD（{settings.futu_host}:{settings.futu_port}）。")
        print("请先启动 OpenD、登录你的【实盘】账户、开启 OpenAPI，然后重跑本脚本。")
        return 1

    import futu  # only the SDK; no trading objects are used

    trd = futu.OpenSecTradeContext(
        filter_trdmarket=getattr(futu.TrdMarket, settings.futu_trd_market),
        host=settings.futu_host,
        port=settings.futu_port,
    )
    out: dict = {"generated_at": date.today().isoformat(), "accounts": []}
    try:
        ret, acc_df = trd.get_acc_list()
        if ret != futu.RET_OK:
            print(f"get_acc_list failed: {acc_df}")
            return 1
        real_accs = acc_df[acc_df["trd_env"].astype(str).str.upper() == "REAL"]
        if real_accs.empty:
            print("没有找到实盘账户（OpenD 里只有模拟盘，或没登录实盘）。")
            print("可用账户：")
            print(acc_df[["acc_id", "trd_env", "acc_type", "security_firm"]].to_string(index=False))
            return 0

        cost_model = build_trade_cost_model(settings)
        for _, acc in real_accs.iterrows():
            acc_id = int(acc["acc_id"])
            rec: dict = {"acc_id": acc_id, "security_firm": str(acc.get("security_firm", "")),
                         "acc_type": str(acc.get("acc_type", ""))}
            print(f"\n=== REAL account {acc_id} ({acc.get('security_firm','')}) ===")

            # account value
            try:
                r, info = trd.accinfo_query(trd_env=futu.TrdEnv.REAL, acc_id=acc_id, currency=futu.Currency.USD)
                if r == futu.RET_OK and not info.empty:
                    row = info.iloc[0]
                    rec["total_assets"] = float(row.get("total_assets", 0) or 0)
                    rec["cash"] = float(row.get("cash", 0) or 0)
                    rec["market_val"] = float(row.get("market_val", 0) or 0)
                    print(f"  total_assets={rec['total_assets']:,.2f} cash={rec['cash']:,.2f} market_val={rec['market_val']:,.2f}")
                else:
                    print(f"  accinfo_query: {info}")
            except Exception as exc:  # noqa: BLE001
                print(f"  accinfo_query error: {exc}")

            # current holdings (with unrealized P&L straight from the broker)
            holdings = []
            try:
                r, pos = trd.position_list_query(trd_env=futu.TrdEnv.REAL, acc_id=acc_id)
                if r == futu.RET_OK and not pos.empty:
                    for _, p in pos.iterrows():
                        holdings.append({
                            "code": str(p.get("code", "")),
                            "qty": float(p.get("qty", 0) or 0),
                            "cost_price": float(p.get("cost_price", 0) or 0),
                            "nominal_price": float(p.get("nominal_price", 0) or 0),
                            "market_val": float(p.get("market_val", 0) or 0),
                            "unrealized_pl": float(p.get("pl_val", 0) or 0),
                            "unrealized_pl_ratio": float(p.get("pl_ratio", 0) or 0),
                        })
                print(f"  持仓 {len(holdings)} 只, 合计未实现盈亏 {sum(h['unrealized_pl'] for h in holdings):,.2f}")
            except Exception as exc:  # noqa: BLE001
                print(f"  position_list_query error: {exc}")
            rec["holdings"] = holdings

            # filled order history -> realized P&L (FIFO per symbol)
            filled = []
            seen = set()
            today = date.today()
            y, m = 2026, 1
            while (y, m) <= (today.year, today.month):
                start = date(y, m, 1).isoformat()
                end = (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)
                try:
                    r, h = trd.history_order_list_query(start=start, end=end.isoformat(),
                                                        trd_env=futu.TrdEnv.REAL, acc_id=acc_id)
                    if r == futu.RET_OK and not h.empty:
                        for _, o in h.iterrows():
                            oid = str(o.get("order_id", ""))
                            dq = float(o.get("dealt_qty", 0) or 0)
                            dp = float(o.get("dealt_avg_price", 0) or 0)
                            if dq > 0 and dp > 0 and oid not in seen:
                                seen.add(oid)
                                filled.append({"ts": str(o.get("create_time", "")), "code": str(o.get("code", "")),
                                               "side": _side(o.get("trd_side", "")), "qty": dq, "price": dp})
                except Exception as exc:  # noqa: BLE001
                    print(f"  history {start[:7]} error: {exc}")
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)

            filled.sort(key=lambda x: x["ts"])
            lots = defaultdict(deque)
            per_symbol = defaultdict(lambda: {"gross": 0.0, "fees": 0.0})
            for f in filled:
                fee = float(getattr(estimate_trade_cost(f["side"], f["qty"], f["price"], timestamp=None, model=cost_model), "total", 0.0) or 0.0)
                a = per_symbol[f["code"]]; a["fees"] += fee
                if f["side"] == "BUY":
                    lots[f["code"]].append([f["qty"], f["price"]])
                elif f["side"] == "SELL":
                    rem = f["qty"]
                    while rem > 1e-9 and lots[f["code"]]:
                        lot = lots[f["code"]][0]; mm = min(rem, lot[0])
                        a["gross"] += mm * (f["price"] - lot[1]); rem -= mm; lot[0] -= mm
                        if lot[0] <= 1e-9:
                            lots[f["code"]].popleft()
            rec["filled_orders"] = len(filled)
            rec["order_date_range"] = [filled[0]["ts"][:10], filled[-1]["ts"][:10]] if filled else []
            rec["realized_gross"] = round(sum(v["gross"] for v in per_symbol.values()), 2)
            rec["realized_fees_est"] = round(sum(v["fees"] for v in per_symbol.values()), 2)
            rec["realized_net_est"] = round(rec["realized_gross"] - rec["realized_fees_est"], 2)
            rec["per_symbol_realized"] = {k: {"gross": round(v["gross"], 2), "fees_est": round(v["fees"], 2)}
                                          for k, v in sorted(per_symbol.items(), key=lambda x: -x[1]["gross"])}
            print(f"  历史成交 {len(filled)} 笔, 已实现 毛利 {rec['realized_gross']:,.0f} 净利(估) {rec['realized_net_est']:,.0f}")
            out["accounts"].append(rec)
    finally:
        trd.close()

    import os
    os.makedirs("runtime", exist_ok=True)
    json.dump(out, open("runtime/real_account_snapshot.json", "w"), ensure_ascii=False, indent=2)
    print("\n写出: runtime/real_account_snapshot.json （Claude 能直接读，跑完说一声即可）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
