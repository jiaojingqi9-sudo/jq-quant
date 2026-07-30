# Stock Strategy Learning Lab

The learning lab is the stock system's evidence-driven strategy evolution layer.
It does not directly modify live-trading parameters. It records decisions,
labels realized outcomes, explains profit/loss drivers, proposes research
candidates, and applies promotion gates before anything can reach paper/live.

## Design Principles

- Learn from execution-level facts, not stories.
- Treat every candidate as guilty until replay, sample-out validation, paper
  trading and manual approval say otherwise.
- Keep live auto-promotion disabled.
- Prefer small, reversible parameter proposals over code-generating strategy
  rewrites.
- Separate signal quality from execution quality whenever possible.

## Data Flow

1. Auto trader writes planned/submitted order decisions to
   `runtime/stock_order_memory.jsonl`.
2. Broker order history is converted into incremental fills in
   `runtime/stock_fills.jsonl`.
3. FIFO realized round trips are written to `runtime/stock_trade_outcomes.jsonl`.
4. Attribution is aggregated into `runtime/stock_attribution.json`.
5. Candidate changes are written to `runtime/strategy_upgrade_candidates.jsonl`.
6. Promotion decisions are written to `runtime/strategy_promotion_report.json`.
7. A human/Codex review packet is written to
   `runtime/stock_learning_review_packet.md` and
   `runtime/stock_learning_review_packet.json`.

## Current Outcome Labels

- `profitable`: net PnL is positive after fees.
- `fees_dominated`: fees dominate gross edge and net PnL is non-positive.
- `signal_error`: price moved against the completed round trip before costs.
- `early_exit_or_noise`: short holding period with negative net PnL.
- `low_edge_trade`: tiny return where fees matter materially.
- `attribution_ambiguous`: strategy ownership is shared or unknown.
- `unmatched_sell`: sell fill has no matching opening lot in the local epoch.

## Candidate Types

- `collect_more_data`: sample is too small for reliable automation.
- `raise_min_order_value`: small trades appear fee-dominated.
- `raise_min_hold`: quick exits appear noisy or loss-making.
- `tighten_entry_threshold`: a strategy has enough losing evidence to test
  stricter entry.
- `review_strategy_allocation`: strategy-level evidence is weak but not tied to
  a known parameter.
- `review_universe_symbol`: symbol-level contribution is persistently negative.

## Promotion Policy

The lab can mark a candidate as `eligible_for_paper_replay`. It cannot mark a
candidate as live-approved. Live promotion requires:

- walk-forward replay
- purged or embargoed validation
- cost/slippage stress
- paper trading
- manual approval

## Human Review Packet

The review packet is the handoff file for code changes. The system may learn,
label and propose, but it cannot edit strategy code. When a candidate looks
interesting, send `runtime/stock_learning_review_packet.md` to Codex and ask for
a review. The paired JSON file includes artifact paths and SHA-256 hashes so the
review can tie conclusions back to raw evidence.

The packet asks the reviewer to check:

- sample size and possible data-snooping
- cost, slippage, partial-fill and execution effects
- candidate evidence versus out-of-sample validation needs
- whether the next step should be replay, paper, or a stock-only code change
- that crypto-system files remain untouched

## Next Research Upgrades

- Add MFE/MAE labels from intraday quote history.
- Add purged/embargoed cross-validation and explicit PBO scoring.
- Add execution-scheduler candidates, not just signal-parameter candidates.
- Add LLM-generated narratives that must cite exact rows from attribution data.
- Add canary state management for tiny live allocation tests.
