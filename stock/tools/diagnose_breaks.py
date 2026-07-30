"""One-off diagnostic: print the exact stock-ledger reconciliation breaks.

Run from the trade/ folder with the project venv:

    cd <repo root>
    .venv/bin/python stock/tools/diagnose_breaks.py

Read-only. Connects to Futu OpenD to read positions + account, rebuilds the
double-entry journal from runtime/stock_fills.jsonl since the current epoch, and
prints each reconciliation break in detail (cash + per-symbol quantity).
"""

from __future__ import annotations

from taa_futu.config import load_settings
from taa_futu.futu_gateway import FutuPaperTrader
from taa_futu.stock_runtime import STOCK_FILLS_FILE, STOCK_LEDGER_EPOCH_FILE, load_stock_ledger_epoch
from taa_futu.stock_ledger import build_stock_double_entry_ledger, reconcile_stock_ledger


def _broker_positions(positions) -> dict[str, float]:
    out: dict[str, float] = {}
    if positions is None or getattr(positions, "empty", True):
        return out
    if "code" not in positions.columns:
        return out
    qty_col = "qty" if "qty" in positions.columns else "quantity"
    for _, row in positions.iterrows():
        try:
            out[str(row["code"]).upper()] = float(row.get(qty_col, 0) or 0)
        except (TypeError, ValueError):
            pass
    return out


def main() -> int:
    settings = load_settings()
    epoch = load_stock_ledger_epoch()
    journal = build_stock_double_entry_ledger(STOCK_FILLS_FILE, epoch_path=STOCK_LEDGER_EPOCH_FILE)

    print("=" * 70)
    print("LEDGER (internal, from fills since epoch)")
    print("-" * 70)
    print(f"epoch_ts            : {epoch.get('ts', 'none')}")
    print(f"fills_at_epoch      : {epoch.get('fills_count_at_reset', 0)}")
    print(f"trade_count         : {journal.trade_count}")
    print(f"chain_valid         : {journal.chain_valid}")
    print(f"net_realized_pnl    : {journal.net_realized_pnl:,.2f}")
    print(f"fees_paid (EST)     : {journal.fees_paid:,.2f}")
    print(f"cash_delta          : {journal.cash_delta:,.2f}")
    if journal.warnings:
        print(f"warnings ({len(journal.warnings)}):")
        for w in journal.warnings[:12]:
            print(f"   - {w}")

    try:
        with FutuPaperTrader(settings) as trader:
            acc_id = trader.resolve_trade_account()
            positions = trader.get_positions(acc_id)
            account = trader.get_account_info(acc_id)
    except Exception as exc:  # noqa: BLE001
        print(f"\n!! could not reach Futu account ({type(exc).__name__}: {exc})")
        print("   Make sure Futu OpenD is running and logged in, then re-run.")
        return 1

    recon = reconcile_stock_ledger(journal, positions=positions, account=account, epoch=epoch)
    broker_qty = _broker_positions(positions)
    proj_qty = dict(journal.positions)

    print("\n" + "=" * 70)
    print("POSITIONS: projected (ledger)  vs  broker (account)")
    print("-" * 70)
    print(f"{'symbol':<10}{'ledger_qty':>14}{'broker_qty':>14}{'diff':>14}")
    for sym in sorted(set(proj_qty) | set(broker_qty)):
        p = proj_qty.get(sym, 0.0)
        b = broker_qty.get(sym, 0.0)
        flag = "   <-- DIFF" if abs(b - p) > 1e-6 else ""
        print(f"{sym:<10}{p:>14.4f}{b:>14.4f}{b - p:>14.4f}{flag}")

    snap = (epoch or {}).get("account_snapshot", {}) or {}
    start_cash = float(snap.get("cash", 0) or 0)
    try:
        actual_cash = float(account.get("cash", account.get("cash_balance", 0)) or 0)
    except Exception:  # noqa: BLE001
        actual_cash = 0.0
    expected_cash = start_cash + journal.cash_delta
    print("\n" + "=" * 70)
    print("CASH: expected (epoch_cash + cash_delta)  vs  broker")
    print("-" * 70)
    print(f"epoch_start_cash    : {start_cash:,.2f}")
    print(f"+ cash_delta        : {journal.cash_delta:,.2f}")
    print(f"= expected_cash     : {expected_cash:,.2f}")
    print(f"broker_cash         : {actual_cash:,.2f}")
    print(f"difference          : {actual_cash - expected_cash:,.2f}")

    print("\n" + "=" * 70)
    print(f"RECONCILIATION: ok={recon.ok}   breaks={len(recon.breaks)}")
    print("-" * 70)
    for b in recon.breaks:
        print(f"[{b.kind:<12}] {b.symbol:<8} expected={b.expected:,.4f}  "
              f"actual={b.actual:,.4f}  diff={b.difference:,.4f}")
    if not recon.breaks:
        print("(no breaks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
