# Crypto USD-M Futures Long/Short Sleeve

This sleeve adds short-capable crypto trading without changing the Spot OFIM system.

## Scope

- Market data: Binance USD-M Futures public mainnet REST (`https://fapi.binance.com`).
- Default execution: local engineering-grade perpetual paper ledger.
- Optional test execution: Binance USD-M Futures Testnet REST (`https://demo-fapi.binance.com`) when the region/account supports it.
- Live futures trading: intentionally unsupported.
- Ledger: independent signed futures ledger under `runtime/crypto_perp/`.
- Positions: signed quantity and signed target weight. Positive means long; negative means short.

## Commands

```bash
.venv/bin/python -m taa_futu.cli crypto-perp-check
.venv/bin/python -m taa_futu.cli crypto-perp-status
.venv/bin/python -m taa_futu.cli crypto-perp-reset
.venv/bin/python -m taa_futu.cli crypto-perp-once
.venv/bin/python -m taa_futu.cli crypto-perp-once --submit
.venv/bin/python -m taa_futu.cli crypto-perp-auto --submit --poll-seconds 120
```

`--submit` in `CRYPTO_PERP_MODE=paper` writes local signed futures fills. `--submit` in `CRYPTO_PERP_MODE=testnet` requires `CRYPTO_PERP_API_KEY` and `CRYPTO_PERP_API_SECRET` from Binance USD-M Futures Testnet.

## Safety Defaults

- Default leverage is `1x`.
- Default margin mode is `ISOLATED`.
- Default order style is `CRYPTO_PERP_ORDER_STYLE=maker_limit`: paper orders are posted as pending maker limits and only fill when a later book/trade crosses the limit price; Binance Futures Testnet uses `LIMIT` with `timeInForce=GTX`.
- Default active capital is `15%` of account equity.
- Default gross exposure is `12%` of active capital.
- Default per-symbol absolute weight is `12%`.
- Default max concurrent symbols is `1`.
- Default entry threshold is `0.32`; default exit threshold is `0.12`.
- New entries and sign flips require `CRYPTO_PERP_SIGNAL_CONFIRM_CYCLES=3` consecutive cycles.
- New entries and sign flips also pass a cost gate by default: `expected_edge_bps = abs(score) * CRYPTO_PERP_EDGE_BPS_PER_SCORE` must cover round-trip taker fees, configured slippage, spread, adverse funding, and `CRYPTO_PERP_COST_BUFFER_BPS`.
- Weak exits require `CRYPTO_PERP_EXIT_CONFIRM_CYCLES=3` consecutive cycles before closing. Adverse funding still exits immediately.
- A perp loss guard blocks new entries after account-level loss, fee drag, total trade count, recent trades, or recent side flips breach `CRYPTO_PERP_LOSS_GUARD_*` limits. Existing positions can still produce reduce-only exits.
- Default minimum trade interval is `600` seconds per symbol.
- Sign flips close the existing position first only after confirmation, then wait for a later cycle to open the opposite direction.
- Local fills use visible order-book VWAP capped by `CRYPTO_PERP_MAX_ORDER_BOOK_TAKE_RATIO=0.10`.
- Pending maker orders expire after `CRYPTO_PERP_MAKER_ORDER_TTL_SECONDS=180`.
- Local equity is marked with public USD-M mark price when available.
- Funding accrues from public `lastFundingRate`; positive net `funding_paid` means the sleeve paid funding, negative means it received funding.
- Local liquidation distance is estimated from entry price, leverage, and maintenance-margin rate.
- Fees use Binance's signed USD-M commission endpoint when available; otherwise the configured fallback `CRYPTO_PERP_FEE_RATE` is used.
