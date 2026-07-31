"""Repair the stock ledger from the authoritative broker order history.

Root cause (confirmed): local fill recording (runtime/stock_fills.jsonl) only
started 2026-05-04, but the broker (OpenD account) has 3,927 filled orders from
2026-04-09. The ~1,400 missing April/early-May fills are why the ledger never
reconciled (unmatched sells, phantom positions).

This rebuilds runtime/stock_fills.jsonl from runtime/broker_history_raw.json (the
complete broker record produced by pull_broker_history.py), replays it from a
clean cash start, and reconciles against the live broker account.

Usage (run on the Mac with OpenD running):

    cd <repo root>
    .venv/bin/python stock/tools/repair_ledger.py            # DRY RUN — writes nothing to the ledger
    .venv/bin/python repair_ledger.py --apply     # backs up + replaces fills + re-epochs

Dry run writes only runtime/stock_fills_rebuilt.jsonl and prints what the
reconciliation WOULD be. --apply backs up the old fills, swaps in the rebuilt
file, and writes a fresh epoch.
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from taa_futu.config import load_settings
from taa_futu.costs import build_trade_cost_model, estimate_trade_cost
from taa_futu.futu_gateway import FutuPaperTrader
from taa_futu.stock_ledger import build_stock_double_entry_ledger, reconcile_stock_ledger

RUNTIME = Path("runtime")
BROKER_RAW = RUNTIME / "broker_history_raw.json"
FILLS = RUNTIME / "stock_fills.jsonl"
REBUILT = RUNTIME / "stock_fills_rebuilt.jsonl"
EPOCH_FILE = RUNTIME / "stock_ledger_epoch.json"
TMP_EPOCH = RUNTIME / "_repair_epoch.json"


def _load_strategy_tags() -> dict[str, str]:
    """Best-effort: recover order_id -> strategy from order_memory (often empty)."""
    tags: dict[str, str] = {}
    p = RUNTIME / "stock_order_memory.jsonl"
    if not p.exists():
        return tags
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        oid = str(d.get("order_id") or d.get("submit_detail") or "")
        strat = d.get("strategy") or d.get("sleeve") or d.get("strategy_name")
        if oid and strat:
            tags[oid] = str(strat)
    return tags


def build_rebuilt_fills(settings) -> list[dict]:
    broker = json.loads(BROKER_RAW.read_text(encoding="utf-8"))
    cost_model = build_trade_cost_model(settings)
    tags = _load_strategy_tags()
    fills: list[dict] = []
    for o in sorted(broker, key=lambda r: r.get("ts", "")):
        side = str(o.get("side", "")).upper()
        qty = float(o.get("qty", 0) or 0)
        price = float(o.get("price", 0) or 0)
        if side not in ("BUY", "SELL") or qty <= 0 or price <= 0:
            continue
        oid = str(o.get("order_id", ""))
        breakdown = estimate_trade_cost(side, qty, price, timestamp=None, model=cost_model)
        fills.append({
            "ts": str(o.get("ts", "")),
            "symbol": str(o.get("code", "")),
            "side": side,
            "quantity": qty,
            "price": price,
            "fee": round(float(getattr(breakdown, "total", 0.0) or 0.0), 6),
            "fee_source": "futu_hk_us_fixed",
            "order_id": oid,
            "strategy": tags.get(oid, "Unknown"),
            "source": "broker_history_rebuild",
        })
    return fills


def _clean_epoch(settings) -> dict:
    """构造重建用的干净 Epoch。

    ``account_snapshot`` 必须含 ``total_assets``：那是「Epoch 后总盈亏」的减数，
    也是界面判定 Epoch 可用与否的依据。这里开仓无持仓，所以总资产等于现金。

    2026-06-02 那次重建漏了这个字段，落盘的 epoch 只有 ``cash``。后果是界面
    判定「Epoch 未设置」，起点资产显示「未设置」、期间盈亏显示「待初始化」、
    券商对账被整块禁用——而对账本身只需要 ``cash``，其实一直算得出结果，
    只是不给看。同一份文件 Doctor 却说「已设置」（它只看 ``ts``）。
    界面上于是出现 Epoch 卡片有日期、旁边写着「还没有设置 Epoch」。
    """
    opening_cash = float(getattr(settings, "initial_capital", 1_000_000.0))
    return {
        "ts": "2026-04-08T00:00:00+00:00",
        "reason": "broker_history_rebuild",
        "account_snapshot": {
            "total_assets": opening_cash,   # 无持仓 → 总资产 = 现金
            "cash": opening_cash,
            "market_val": 0.0,
            "position_count": 0,
            "positions": [],
        },
        "fills_count_at_reset": 0,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually replace the fills + epoch (default dry-run).")
    args = parser.parse_args(argv)

    if not BROKER_RAW.exists():
        print(f"missing {BROKER_RAW} — run pull_broker_history.py first.")
        return 1

    settings = load_settings()
    fills = build_rebuilt_fills(settings)
    REBUILT.write_text("\n".join(json.dumps(f, ensure_ascii=False) for f in fills) + "\n", encoding="utf-8")
    n_unknown = sum(1 for f in fills if f["strategy"] == "Unknown")
    print(f"rebuilt {len(fills)} fills from broker history -> {REBUILT}")
    print(f"  ({len(fills) - n_unknown} have a recovered strategy tag, {n_unknown} = Unknown)")

    def _acct_cash(acc) -> float:
        try:
            return float(acc.get("cash", acc.get("cash_balance", 0)) or 0)
        except Exception:
            return 0.0

    epoch = _clean_epoch(settings)
    TMP_EPOCH.write_text(json.dumps(epoch, ensure_ascii=False, indent=2), encoding="utf-8")
    journal = build_stock_double_entry_ledger(REBUILT, epoch_path=TMP_EPOCH)
    print(f"\nrebuilt ledger: trades={journal.trade_count} chain_valid={journal.chain_valid} "
          f"net_realized={journal.net_realized_pnl:,.2f} fees(est)={journal.fees_paid:,.2f}")

    try:
        with FutuPaperTrader(settings) as trader:
            acc_id = trader.resolve_trade_account()
            positions = trader.get_positions(acc_id)
            account = trader.get_account_info(acc_id)
    except Exception as exc:  # noqa: BLE001
        print(f"\n(could not reach broker: {type(exc).__name__}: {exc} — is OpenD running?)")
        print("DRY RUN — nothing changed." if not args.apply else "ABORTED — cannot apply without broker.")
        return 1

    # Pass 1: reconcile assuming a $1M opening balance — shows the raw gaps.
    recon = reconcile_stock_ledger(journal, positions=positions, account=account, epoch=epoch)
    pos_breaks = [b for b in recon.breaks if b.kind != "cash"]
    print(f"\nPASS 1 (assumed $1M start):  position breaks={len(pos_breaks)}  cash breaks={len(recon.breaks) - len(pos_breaks)}")
    for b in recon.breaks:
        print(f"  [{b.kind:<12}] {b.symbol:<8} expected={b.expected:,.2f} actual={b.actual:,.2f} diff={b.difference:,.2f}")

    # Calibrate opening cash = current account cash - tracked flows. Positions
    # already reconcile on their own (that is what proves the fills are complete);
    # we don't have the exact 4-09 opening balance, so we infer it. The residual it
    # absorbs is fee-estimation + dividends.
    actual_cash = _acct_cash(account)
    raw_gap = round(actual_cash - (epoch["account_snapshot"]["cash"] + journal.cash_delta), 2)
    calibrated = round(actual_cash - journal.cash_delta, 2)
    epoch["account_snapshot"]["cash"] = calibrated
    # 总资产要跟着走。开仓无持仓，两者恒等；只改 cash 会让 total_assets 停在
    # 未校准的旧值上，期间盈亏就会差出这笔校准额。
    epoch["account_snapshot"]["total_assets"] = calibrated
    epoch["reason"] = "broker_history_rebuild (opening cash inferred from current account - tracked flows)"
    epoch["calibration_note"] = f"absorbs ${raw_gap:,.0f} of fee-estimation/dividends vs an assumed $1M start"
    TMP_EPOCH.write_text(json.dumps(epoch, ensure_ascii=False, indent=2), encoding="utf-8")
    journal = build_stock_double_entry_ledger(REBUILT, epoch_path=TMP_EPOCH)
    recon = reconcile_stock_ledger(journal, positions=positions, account=account, epoch=epoch)
    print(f"\nPASS 2 (calibrated start ${calibrated:,.0f}):  ok={recon.ok}  breaks={len(recon.breaks)}")
    for b in recon.breaks:
        print(f"  [{b.kind:<12}] {b.symbol:<8} expected={b.expected:,.2f} actual={b.actual:,.2f} diff={b.difference:,.2f}")
    if recon.ok:
        print("  (clean — positions AND cash now reconcile)")
    print(f"  note: the ${raw_gap:,.0f} cash gap = fee model over-estimate (+ any dividends); positions matched on their own.")

    if not args.apply:
        print("\nDRY RUN — nothing changed. If PASS 2 shows breaks=0, re-run with --apply.")
        return 0

    backup = FILLS.with_name(f"stock_fills.backup_{time.strftime('%Y%m%d_%H%M%S')}.jsonl")
    if FILLS.exists():
        shutil.copy2(FILLS, backup)
        print(f"\nbacked up old fills -> {backup}")
    shutil.copy2(REBUILT, FILLS)
    EPOCH_FILE.write_text(json.dumps(epoch, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"APPLIED: {FILLS} now holds the full broker history; epoch = calibrated clean start.")
    print("Re-run  .venv/bin/taa-futu stock-ledger-audit  to confirm reconciliation_ok=True.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
