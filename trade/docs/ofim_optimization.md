# OFIM Optimization (walk-forward, research-only)

## Conclusion first

`ofim_research_loop.py` searches a small OFIM parameter grid on a **train**
window, validates survivors on an **out-of-sample** window, and reports one
unbiased number on a **locked test** window. The only number you should trust is
the locked-test number — tuning until the in-sample backtest is green is
data-snooping, which the loop is explicitly built to resist. It never edits live
parameters; a winner can only reach live via `strategy_overrides.py` after your
review.

## What was verified (sandbox, real LOB replay)

- The OFIM replay (`run_ofim_replay`) runs deterministically on your stored
  40-level LOB data (`runtime/market_data/<date>/`), cost-aware with the same fee
  model as live. 76 trading days are available (2026-03-11 → 2026-06-01).
- **The core profitability leak is over-trading / fees, not bad signals.** In the
  live attribution, fees were ~46% of gross P&L (8,119 of ~17,800). On any single
  day fees look tiny; the drag only shows up cumulatively — which is exactly why a
  multi-day walk-forward (not a one-day backtest) is the right tool.
- **Anti-churn controls cut trades hard.** A 2% rebalance-drift gate took one
  active day from 37 trades to 3 (fees $91 → $25) with a slightly better net —
  most churn was micro-rebalancing tiny weight drifts. Note: the live auto-trader
  already throttles rebalancing at `AUTO_TRADER_REBALANCE_DRIFT_PCT` (default 1%),
  but the replay ignored it, so the raw replay *over-stated* live churn. The
  replay now honors a configurable drift gate — both faithful and tunable.
- **The biggest single losses were overnight holds** (enter ~15:5x, exit next
  morning ~09:4x). Overnight gap risk, not intraday signal, drives the fat tail.
  A "flat-by-close" variant is the highest-value hypothesis to test next.
- Directional A/B on an active day (2026-03-13): raising `OFIM_ENTRY_THRESHOLD`
  0.20 → 0.30 cut 37 → 32 trades, fees $91 → $78, and net −0.91% → −0.82%. Small
  on one day; the loop measures whether it holds across many days out-of-sample.

## How to run (on your Mac — heavy days need RAM)

The sandbox here only has ~4 GB RAM, so it can't load the 1–4 GB May days. Run the
full search on your machine:

```bash
cd <repo root>
.venv/bin/python -m taa_futu.ofim_research_loop \
    --train 2026-03-11:2026-04-15 \
    --val   2026-04-17:2026-05-02 \
    --test  2026-05-05:2026-05-29 \
    --max-trials 16
```

Output: a ranking on train, the validation re-scores of the top-K, and the
winner's **locked-test** metrics, written to
`runtime/ofim_research_report.json`. `recommended` is `keep_default` unless a
tuned parameter genuinely beat the default out-of-sample with no overfit flags.

## The no-overnight variant (`--flat-by-close`)

The biggest single losses in the live data were overnight holds (enter near the
close, exit into the next morning's gap). Add `--flat-by-close` to force-liquidate
every position at each day's close, so nothing is carried overnight:

```bash
cd <repo root> && .venv/bin/python -m taa_futu.ofim_research_loop --train 2026-03-11:2026-04-15 --val 2026-04-17:2026-05-02 --test 2026-05-05:2026-05-29 --max-trials 16 --flat-by-close
```

Run it once **without** and once **with** `--flat-by-close`, then compare the two
locked-test numbers (the report's `mode` field records which is which). If
flat-by-close wins, the edge was being given back to overnight gap risk, and the
fix is an execution rule (exit by close) rather than a signal change.

## Parameter grid

Single-axis perturbations around the current defaults (linear, interpretable):

| env var | candidates |
| --- | --- |
| `OFIM_ENTRY_THRESHOLD` | 0.15 / 0.20 / 0.25 / 0.30 / 0.35 |
| `OFIM_EXIT_THRESHOLD` | 0.05 / 0.10 / 0.15 |
| `OFIM_MAX_POSITIONS` | 3 / 5 / 8 |
| `OFIM_MAX_SPREAD_BPS` | 10 / 15 / 20 |
| `OFIM_MIN_VOL_ACCELERATION` | 1.0 / 1.2 / 1.5 |

Plus two **execution-layer anti-churn axes**, searched first because over-trading
is the diagnosed core problem:

| control | candidates | live analog (overridable) |
| --- | --- | --- |
| `min_rebalance_drift_pct` | 0.01 / 0.02 / 0.05 | `AUTO_TRADER_REBALANCE_DRIFT_PCT` |
| `min_hold_cycles` | 10 / 30 / 60 | `AUTO_TRADER_MIN_HOLD_MINUTES` |

The five Settings axes are in the `strategy_overrides` safety whitelist, so a
winner among them promotes without touching any safety switch. The two execution
axes are backtest controls; if one wins, set its live analog (both overridable).
The cycle→minute mapping is approximate, so treat that as directional.

## Closing the loop into live

If the locked-test winner is a tuned parameter and passes the overfit guard:

```bash
# inspect the report, then promote the winning param through the human gate:
python -m taa_futu.strategy_overrides promote --candidate-id <from report> --approved-by you
# or let the loop write the override candidate directly:
.venv/bin/python -m taa_futu.ofim_research_loop ... --write-override
# activate only when you're ready:  STRATEGY_OVERRIDES_ENABLED=true in .env
```

## Honest caveats

- The replay excludes intra-day fill slippage beyond the modeled spread, and the
  single-day-independent path excludes overnight gaps — so replay returns are
  optimistic relative to live.
- A 16-trial single-axis search over ~2 months is a starting point, not proof of
  a profitable strategy. Widen windows, add a flat-by-close variant, and re-run
  the locked test before trusting any change. Simulated results never guarantee
  live returns.
