# Crypto Evidence-to-Review Learning Lab

This is the crypto system's evidence loop. It does not change live parameters,
does not edit code, and does not promote strategy candidates by itself.

## Artifacts

All files live under `runtime/crypto_ofim/`:

- `crypto_order_memory.jsonl`: planned/submitted/cancelled/filled/rejected order
  decision snapshots.
- `crypto_trade_outcomes.jsonl`: FIFO realized outcomes with gross/net PnL,
  fees, slippage, hold time and crypto venue context.
- `crypto_attribution.json`: attribution by strategy, symbol, reason, venue,
  timeframe and 24h market regime.
- `crypto_upgrade_candidates.jsonl`: research-only candidate improvements.
- `crypto_promotion_report.json`: promotion gates. Live promotion is always
  false.
- `crypto_learning_review_packet.md` and
  `crypto_learning_review_packet.json`: Codex review packet with artifact paths,
  SHA-256 hashes, candidates, gates, top winners/losers and checklist.

## Commands

```bash
.venv/bin/taa-futu crypto-learning-build
.venv/bin/taa-futu crypto-learning-export
.venv/bin/taa-futu crypto-learning-status
```

## Safety Policy

- The learning loop may propose `research_only` candidates such as
  `raise_min_order_value`, `tighten_entry_threshold`, `avoid_high_funding`,
  `reduce_symbol_weight`, `pause_symbol_or_venue`, or
  `improve_execution_scheduler`.
- The learning loop must not change `.env`, live strategy code, or exchange
  settings.
- If evidence is ambiguous, the correct output is a review packet, not a risky
  default.

## Crypto-Specific Evidence

The order memory and outcomes include crypto fields where available:

- exchange / venue
- spot vs perpetual fields (`instrument_type`, `product_type`)
- leverage and margin mode
- liquidation distance / margin risk placeholders
- funding rate / funding paid
- maker/taker fee classification
- slippage from expected vs executed price
- 24h market regime proxy from benchmark score
- exchange latency, rejection, partial fill and stale-book flags
- inventory exposure and max drawdown placeholders

Spot Binance currently uses `leverage=1`, `margin_mode=none`, and `funding=0`.
Those fields are kept in the schema so perpetual venues can be added later
without changing the review packet shape.

## How To Review With Codex

Send `runtime/crypto_ofim/crypto_learning_review_packet.md` to Codex and ask it
to inspect the evidence, promotion gates, overfitting risk, costs, slippage,
latency/rejection/partial-fill data and candidate rationale. Code changes should
only happen after a separate human-approved implementation plan and tests.
