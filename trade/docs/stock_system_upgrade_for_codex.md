# Stock Trading System Upgrade Instructions for Codex

> This document describes a set of prioritized engineering improvements to the stock trading system (`taa_futu`). Each section is self-contained and can be implemented independently. Do not break existing functionality — all changes should be additive or backward-compatible unless explicitly stated.

---

## Context

The stock trading system (`auto_trader.py`, `watchdog.py`, `dashboard_app.py`, `futu_gateway.py`, `costs.py`, etc.) works but lags behind the crypto trading system (`crypto_ofim.py`, `crypto_ofim_stream.py`, `crypto_ofim_watchdog.py`, `crypto_ofim_app.py`) in several key areas:

1. Data acquisition is poll-based (60s REST) vs. stream-based (100ms WebSocket)
2. Fill events are not persisted to an append-only log
3. No ledger epoch / clean accounting reset mechanism
4. Exit protection is time-based only, not signal-confirmation-based
5. Trade cooldown is global, not per-symbol
6. No ledger audit UI in dashboard
7. No error log display in dashboard

---

## Priority 1 — Core Architecture (High Impact)

### 1.1 Persist Fill Events to an Append-Only Log

**Goal:** Every stock fill should be written to `runtime/stock_fills.jsonl` atomically, the same way crypto writes to `runtime/crypto_ofim/user_fills.jsonl`. This enables reliable ledger reconstruction from an event log rather than re-querying the broker API each time.

**Files to modify:** `auto_trader.py`, `costs.py`

**New file:** `runtime/stock_fills.jsonl` (runtime artifact, not source code)

**Implementation:**

In `auto_trader.py`, add a new module-level set `_recorded_fill_ids: set[str]` (or store on `AutoTraderState`) to track already-persisted fill event IDs. After each successful `submit_orders()` call, and also on every poll cycle during market hours, call `_record_new_fills(trader, acc_id, state)`:

```python
def _record_new_fills(trader: FutuPaperTrader, acc_id: int, state: AutoTraderState) -> None:
    """Fetch order history, diff against known fill IDs, append new fills to stock_fills.jsonl."""
    now_utc = datetime.now(UTC)
    market_date = _market_now(now_utc, settings).date()
    start = (market_date - timedelta(days=1)).isoformat()
    end = market_date.isoformat()
    try:
        history = trader.get_order_history(acc_id, start, end)
    except Exception:
        return
    filled = _filled_orders(history)
    if filled.empty:
        return
    fills_path = RUNTIME_DIR / "stock_fills.jsonl"
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    for _, row in filled.iterrows():
        event_id = str(row.get("order_id", ""))
        if not event_id or event_id in state.recorded_fill_ids:
            continue
        broker_fee = broker_fee_total_from_row(row)
        fee = broker_fee if broker_fee is not None else 0.0
        fee_source = "broker_reported" if broker_fee is not None else "estimated"
        record = {
            "ts": str(row.get("updated_time") or row.get("create_time") or now_utc.isoformat()),
            "symbol": str(row.get("code", "")),
            "side": str(row.get("trd_side", "")).upper(),
            "quantity": float(row.get("dealt_qty_num", 0.0)),
            "price": float(row.get("dealt_price_num", 0.0)),
            "fee": fee,
            "fee_source": fee_source,
            "event_id": event_id,
            "source": "futu_order_history",
        }
        with fills_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        state.recorded_fill_ids.add(event_id)
```

Add `recorded_fill_ids: set` to `AutoTraderState.__post_init__` (default `set()`). Load existing IDs from `stock_fills.jsonl` at startup to avoid re-recording.

In `costs.py`, add a new function `build_stock_fills_ledger(fills_path: Path) -> LedgerProjection` that reads `stock_fills.jsonl` and calls `project_fills()`. This replaces the current `estimate_realized_from_fills()` (keep the old one for backward compatibility but mark it as deprecated).

---

### 1.2 Add Ledger Epoch Mechanism

**Goal:** Enable a clean accounting reset so realized PnL is always computed from a known starting point, not "since the beginning of time". Mirrors `ledger_epoch.json` in the crypto system.

**New file:** `runtime/stock_ledger_epoch.json`

**Files to modify:** `auto_trader.py`, `cli.py`, `dashboard_app.py`

**Epoch file format:**
```json
{
  "ts": "2026-05-03T00:00:00+00:00",
  "reason": "manual_reset",
  "account_snapshot": {
    "total_assets": 1000000.0,
    "cash": 1000000.0,
    "market_val": 0.0
  },
  "fills_count_at_reset": 0
}
```

**Implementation:**

Add a function `write_stock_ledger_epoch(reason: str, account_snapshot: dict) -> Path` in a new helper (or inside `auto_trader.py`). It reads the current line count of `stock_fills.jsonl`, writes the epoch JSON.

In `build_stock_fills_ledger()`, after loading all fills, filter to only include fills whose index >= `fills_count_at_reset` from the epoch file (if it exists).

In `cli.py`, update `cmd_reset_simulate` to also call `write_stock_ledger_epoch(reason="manual_reset", ...)` after clearing the Futu paper account. Add a new CLI command `cmd_stock_status` that reads `auto_trader_status.json` + `watchdog_status.json` and prints a summary table.

---

### 1.3 Improve LOB Data Freshness via Push Subscription

**Goal:** Reduce LOB data staleness from 60s to the OpenD push interval (~1s). Currently `subscribe_push=False` is hardcoded in `futu_gateway.py`, which means LOB is only fetched on-demand once per poll cycle.

**Files to modify:** `futu_gateway.py`, `market_logger.py`

**Implementation:**

In `futu_gateway.py`, add a new method `subscribe_push_lob(symbols: list[str], callback)` that calls `quote_ctx.subscribe(..., subscribe_push=True)` and registers an `on_recv_rsp` style handler. The handler receives push updates and calls `market_logger.log_lob()` for each update.

Create `runtime/lob_cache.json` (analogous to `ws_cache.json` in crypto): an atomically-written file mapping `symbol -> {bids: [...], asks: [...], ts: "..."}`. The push callback overwrites the entry for each symbol. The OFIM signal computation in `ofim_intraday.py` should prefer reading from `lob_cache.json` if it exists and is fresh (age < 5s), falling back to `get_order_book_safe()` otherwise.

This is a non-breaking enhancement — if push subscription fails, the system falls back to the existing poll-based approach.

---

## Priority 2 — Trading Behavior (Medium Impact)

### 2.1 Add Exit Confirm Cycles

**Goal:** Require an exit signal to appear in N consecutive poll cycles before actually generating a SELL order. Prevents exiting a position on a single noisy signal. Mirrors `exit_confirm_cycles` in the crypto system.

**Files to modify:** `auto_trader.py`, `config.py`

**New config key:** `AUTO_TRADER_EXIT_CONFIRM_CYCLES` (int, default `1` = current behavior, recommended `3`)

**Implementation:**

Add to `Settings`:
```python
auto_trader_exit_confirm_cycles: int = 1
```

Add to `AutoTraderState`:
```python
exit_signal_counts: dict  # code -> int, number of consecutive cycles with target_weight == 0
```
Initialize to `{}` in `__post_init__`.

In `run_cycle()`, after computing `stack_target_map`, for each code currently held (in `positions`) where `stack_target_map.get(code, 0.0) == 0`:
- Increment `state.exit_signal_counts[code]`
- If count < `settings.auto_trader_exit_confirm_cycles`, add code to `ignored_symbols` (preventing SELL generation this cycle)
- If count >= threshold, allow SELL and reset counter

For any code where `stack_target_map.get(code, 0.0) > 0` (signal back), reset `state.exit_signal_counts[code] = 0`.

Log confirm count progress: `f"exit-confirm: {code} count={count}/{threshold}, holding position"`

---

### 2.2 Add Per-Symbol Trade Cooldown

**Goal:** Prevent the same symbol from being traded again within N seconds, regardless of overall signal state. Mirrors `min_trade_interval_seconds` in the crypto system. The existing `auto_trader_order_cooldown_seconds` is global (affects all symbols at once); this is per-symbol.

**Files to modify:** `auto_trader.py`, `config.py`

**New config key:** `AUTO_TRADER_MIN_SYMBOL_INTERVAL_SECONDS` (int, default `0` = disabled)

**Implementation:**

Add to `Settings`:
```python
auto_trader_min_symbol_interval_seconds: int = 0
```

Add to `AutoTraderState`:
```python
last_symbol_trade_time: dict  # code -> datetime of last submitted order
```

After `submit_orders()` succeeds, update `state.last_symbol_trade_time[order.code] = now_utc` for each submitted order.

Before adding an order to `planned_orders`, check:
```python
min_interval = timedelta(seconds=settings.auto_trader_min_symbol_interval_seconds)
if min_interval.total_seconds() > 0:
    last_trade = state.last_symbol_trade_time.get(order.code)
    if last_trade and (now_utc - last_trade) < min_interval:
        continue  # skip this order this cycle
```

---

## Priority 3 — UI / Observability (Medium Impact)

### 3.1 Add Ledger Audit Section to Dashboard

**Goal:** Show a clean summary of account performance from the ledger epoch to now. Mirrors `_render_balance_audit()` in `crypto_ofim_app.py`.

**File to modify:** `dashboard_app.py`

**New function:** `_render_ledger_audit(settings, trader)`

Display the following metrics as Streamlit metric cards + a details table:

| Metric | Source |
|---|---|
| Epoch start time | `stock_ledger_epoch.json` → `ts` |
| Epoch start value (USD) | `stock_ledger_epoch.json` → `account_snapshot.total_assets` |
| Current total assets | Live `get_account_info()` |
| Realized PnL (since epoch) | `build_stock_fills_ledger()` → `projection.realized_pnl` |
| Fees paid | `projection.fees_paid` (broker_reported) + estimated delta |
| Net realized (after fees) | `realized_pnl - fees_paid` |
| Unrealized PnL | Live `positions["unrealized_pl"].sum()` |
| Total P&L | Net realized + Unrealized |

Add a "Reset Epoch" button in a collapsible expander (danger zone), equivalent to the liquidate+reset workflow in crypto.

Add this section to the sidebar nav or as a new top-level tab in the main dashboard.

---

### 3.2 Add Error Log Display to Dashboard

**Goal:** Surface errors from `market_data/{date}/errors.jsonl` in the UI so operational issues are visible without log file access.

**File to modify:** `dashboard_app.py`

**Implementation:**

In `_render_tables()` or a new dedicated section, add an "Errors" tab. Read the last 3 days of `errors.jsonl` via `market_logger.load_records("errors", start, end)`. Display as a table with columns: `ts`, `context`, `detail` (truncated to 200 chars with expand button).

Add a red badge to the "Status" section header if any errors occurred in the last 60 minutes: `st.error(f"⚠ {n} errors in the last hour")`.

---

## Priority 4 — Operational Hardening (Low Impact)

### 4.1 Add Data Quality Probe to Watchdog

**Goal:** Detect the case where FutuOpenD is connected but returning bad data (price=0, empty LOB), without restarting on a single bad reading.

**File to modify:** `watchdog.py`

**Implementation:**

Add a `_data_quality_probe(settings: Settings) -> tuple[bool, str]` function that opens a temporary `FutuPaperTrader`, calls `get_snapshots([settings.symbols[0]])`, checks that `last_price > 0`. Retry up to 3 times with 2s sleep. Returns `(True, "ok")` or `(False, "price=0 for {symbol}")`.

In `_run_cycle()`, call this probe only when `market_open=True` and `opend_connected=True`. If probe fails 3 consecutive cycles, include it in the health check result so the auto_trader gets restarted.

Track consecutive probe failures on `WatchdogState.data_quality_failures: int`.

---

### 4.2 Add Error Scrubbing to Futu Error Messages

**Goal:** Prevent API keys, account IDs, and passwords from appearing in log files or error messages.

**File to modify:** `futu_gateway.py`

**Implementation:**

Add a `_scrub_error(message: str, settings: Settings) -> str` function that replaces known sensitive values with `***`:
- `settings.futu_unlock_trade_password_md5` → `"***md5***"`
- `str(settings.futu_acc_id)` → `"***acc_id***"` (if not None)
- Any 32-char hex string → `"***hash***"`

Wrap `FutuTradeError(message)` and `FutuTransientError(message)` raises in `_call_with_retry()` with `_scrub_error(message, self.settings)`.

---

### 4.3 Add `cmd_stock_status` CLI Command

**Goal:** Print a readable summary of the current auto trader and watchdog state, equivalent to `cmd_crypto_ofim_status`.

**File to modify:** `cli.py`

**Implementation:**

```python
def cmd_stock_status(_args: argparse.Namespace) -> None:
    auto_status = {}
    watchdog_status = {}
    auto_path = RUNTIME_DIR / "auto_trader_status.json"
    watchdog_path = RUNTIME_DIR / "watchdog_status.json"
    if auto_path.exists():
        auto_status = json.loads(auto_path.read_text())
    if watchdog_path.exists():
        watchdog_status = json.loads(watchdog_path.read_text())
    rows = [
        ["auto_trader.running", auto_status.get("running", "—")],
        ["auto_trader.action", auto_status.get("action", "—")],
        ["auto_trader.detail", str(auto_status.get("detail", "—"))[:80]],
        ["auto_trader.updated_at", auto_status.get("updated_at", "—")],
        ["watchdog.running", watchdog_status.get("running", "—")],
        ["watchdog.action", watchdog_status.get("action", "—")],
        ["watchdog.opend_connected", watchdog_status.get("opend_connected", "—")],
        ["watchdog.restart_count", watchdog_status.get("restart_count", "—")],
    ]
    _print_table(rows, ["key", "value"])
```

Register it in the subparser setup alongside the existing stock commands.

---

## Summary of New Config Keys

Add these to `.env` (all have safe defaults that preserve existing behavior):

```env
# Exit confirmation: require signal to disappear N consecutive cycles before exiting
AUTO_TRADER_EXIT_CONFIRM_CYCLES=1

# Per-symbol trade cooldown in seconds (0 = disabled)
AUTO_TRADER_MIN_SYMBOL_INTERVAL_SECONDS=0
```

---

## Summary of New Runtime Files

| File | Purpose |
|---|---|
| `runtime/stock_fills.jsonl` | Append-only fill event log (mirrors crypto's user_fills.jsonl) |
| `runtime/stock_ledger_epoch.json` | Accounting start point for PnL calculation |
| `runtime/lob_cache.json` | Latest LOB snapshot per symbol (written by push handler) |

---

## Implementation Order (Recommended)

1. `stock_fills.jsonl` persistence (1.1) — foundational, everything else builds on it
2. Ledger epoch mechanism (1.2) — depends on 1.1
3. Exit confirm cycles (2.1) — independent, safe to do in parallel
4. Per-symbol cooldown (2.2) — independent, safe to do in parallel
5. Dashboard ledger audit (3.1) — depends on 1.1 and 1.2
6. Dashboard error log (3.2) — independent
7. LOB push subscription (1.3) — most complex, do last
8. Watchdog data probe (4.1), error scrubbing (4.2), CLI status (4.3) — polish, any order
