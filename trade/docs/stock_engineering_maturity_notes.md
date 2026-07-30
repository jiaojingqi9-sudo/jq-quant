# Stock Trading System Engineering Maturity Notes

This note maps mature trading/accounting ideas to concrete stock-system behavior.
It is intentionally engineering-focused: only practices that can be implemented,
tested, audited, or monitored belong here.

## Sources And Takeaways

- Event sourcing: store state changes as an ordered event log, then rebuild state
  from the log. In this system, `runtime/stock_fills.jsonl` remains the source
  event stream for fills.
- Double-entry accounting: every economic event should create balanced postings.
  In this system, `stock_ledger.py` derives a double-entry journal from stock
  fill events.
- Tamper-evident logging: each journal row carries a previous hash and an event
  hash, forming a hash chain. This does not prevent file deletion, but it makes
  silent row edits or reordering detectable.
- FIX-style execution lifecycle: fills should be execution/increment based, not
  merely order-id based. The auto trader now records cumulative-fill deltas so a
  partially filled order can produce multiple incremental fill records.
- Market-access risk controls: pre-trade checks should cap exposure and order
  size before anything reaches the broker. The auto trader applies target gross,
  per-symbol target, per-order notional, and per-cycle turnover limits.
- Optimal-execution research: large trades should be paced and bounded because
  impact and volatility risk trade off against each other. The current system
  implements hard caps first; a future execution scheduler can split large
  rebalance orders over time.

## Implemented

- Append-only fill log: `runtime/stock_fills.jsonl`
- Ledger epoch: `runtime/stock_ledger_epoch.json`
- Double-entry derived journal: `runtime/stock_journal.jsonl`
- Hash-chain audit hash on every journal entry
- Broker-position reconciliation against projected ledger positions
- Incremental partial-fill recording by cumulative order-history delta
- Dashboard audit metrics and reconciliation breaks
- Epoch-loss guard: once configured loss limits are breached, new-risk orders
  are blocked and only sell/reduce-risk orders are allowed
- Strategy Learning Lab:
  - `runtime/stock_order_memory.jsonl` records planned/submitted order context
  - `runtime/stock_trade_outcomes.jsonl` stores FIFO realized outcome labels
  - `runtime/stock_attribution.json` summarizes PnL by strategy, symbol and reason
  - `runtime/strategy_upgrade_candidates.jsonl` stores research-level upgrade ideas
  - `runtime/strategy_promotion_report.json` records promotion-gate decisions
  - live auto-promotion is intentionally disabled
- CLI commands:
  - `.venv/bin/taa-futu stock-system-doctor`
  - `.venv/bin/taa-futu stock-system-reset`
  - `.venv/bin/taa-futu stock-ledger-status`
  - `.venv/bin/taa-futu stock-ledger-reset`
  - `.venv/bin/taa-futu stock-ledger-audit`

`stock-system-reset` is the preferred glue command: it records one coherent
starting point for both the event/double-entry ledger and the four-strategy
split ledger. `stock-ledger-reset` remains available for low-level ledger-only
maintenance.

`stock-system-doctor` is the preferred glue check: it is read-only and reports
whether runtime state, split accounting, learning packets, auto-trader status,
watchdog status and broker reconciliation are mutually consistent.

## Remaining High-Value Gaps

- Corporate actions: dividends, splits, symbol changes, tax withholding, interest,
  and deposits/withdrawals should become explicit ledger events.
- Execution scheduler: orders above the cap should be split across time instead
  of simply skipped or capped.
- Market calendar: the auto trader currently uses weekday/time-window checks;
  holiday and early-close awareness should be added.
- Drop-copy style reconciliation: if Futu exposes richer deal/execution history,
  use that as the primary fill source instead of cumulative order rows.
- Learning Lab upgrades: add purged/embargoed validation, explicit PBO scoring,
  paper/canary state machines, and richer market-context labels such as MFE/MAE.
