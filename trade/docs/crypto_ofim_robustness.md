# Crypto OFIM robustness notes

This note records the rules used by the crypto OFIM testnet app after the
ledger cleanup.

## Papers and engineering references

- Cont, Kukanov and Stoikov, *The Price Impact of Order Book Events*:
  order-flow imbalance is a more stable short-horizon explanatory variable
  than raw trade volume for price changes. This supports keeping OFIM, but it
  does not imply every small rebalance should be traded.
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1712822
- Zhang, Zohren and Roberts, *DeepLOB*: LOB signals need temporal context and
  multiple book levels. This supports keeping 1-minute bars, ticks and several
  depth tiers rather than using one raw top-of-book imbalance.
  https://arxiv.org/abs/1808.03668
- Bailey, Borwein, Lopez de Prado and Zhu, *The Probability of Backtest
  Overfitting*: high-performing backtests can be false discoveries. This
  supports conservative defaults, walk-forward/replay tests, and no aggressive
  parameter chasing after one good run.
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253
- Martin Fowler, *Event Sourcing*: durable events should be the source of truth
  and account state should be a projection. This is why fills are logged first,
  then positions, realized PnL, fees and audit hashes are derived from the fill
  journal.
  https://martinfowler.com/eaaDev/EventSourcing.html
- Binance Spot API documentation: depth 1-100 costs 5 request weight, recent
  trades cost 25, 1-minute klines cost 2, and IP rate limits must be backed off
  on 429 responses.
  https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints
  https://developers.binance.com/docs/binance-spot-api-docs/rest-api/limits

## Current conservative crypto universe

The default crypto testnet universe is now the tight liquid pool:

- `BTCUSDT`
- `ETHUSDT`
- `SOLUSDT`
- `BNBUSDT`
- `XRPUSDT`

This is intentionally not a "hunt every small coin" setting. A REST-polling
long-only OFIM strategy should first prove that it can survive fees and slippage
on the most liquid pairs. Small coins can jump, but they also add wider spreads,
more false positives, more API load and worse execution.

## Current execution guardrails

- Poll every 15 seconds.
- Use depth 100 by default.
- Hold at most 1 coin at a time.
- Cap one coin at 25% of strategy equity and total spot exposure at 50%.
- Require a higher entry score (`CRYPTO_OFIM_ENTRY_THRESHOLD=0.44`) before
  paying spot taker costs.
- Clamp loaded settings so entry score cannot fall below 0.44, order notional
  cannot fall below 67.5 USDT, single-order notional cannot exceed 2500 USDT,
  spread guard cannot exceed 10.24 bps, same-symbol trade interval cannot fall
  below 600 seconds, and post-risk-off re-entry cooldown cannot fall below
  57600 seconds.
- Scale exposure down under mild benchmark weakness instead of turning off
  trading until the hard risk-off threshold is hit.
- Require stronger exit score filter (`CRYPTO_OFIM_EXIT_THRESHOLD=0.10`).
- Treat volume acceleration as a soft score penalty, not a hard gate.
- Ignore small rebalance noise under 5% of equity.
- Require 4 consecutive empty-signal cycles before normal exit.
- Use a 10-minute stale-hold guard to prevent positions from getting stuck
  after the signal disappears.
- Use same-coin, side-flip and post-risk-off re-entry cooldowns by default.
- Watchdog compares the running spot process' reported strategy settings with
  freshly loaded guardrails. If a loss guard is already holding the process at
  zero orders, it reports every stale or looser setting instead of restarting
  into a guarded loop.

This is "high-frequency observation, lower-frequency execution." The app still
looks at market data frequently, but it avoids paying fees on signal jitter.

## Ledger rule

Binance Spot Testnet accounts may contain many faucet assets. The app separates:

- Quote cash: `USDT`.
- Active universe assets: currently the five liquid symbols above.
- Historically traded assets: symbols previously touched by the OFIM app.
- Testnet unused assets: faucet/test balances that the strategy does not own.

Strategy equity counts only `USDT` plus strategy-explained current positions.
Unused testnet coins are shown in the balance audit table but do not affect
strategy PnL.

## Implemented robustness layer

The first large refactor slice is now implemented without changing the stock
system:

- Every auto/manual cycle writes a best-effort event journal at
  `runtime/crypto_ofim/events.jsonl`.
- The event journal includes cycle start/end, market snapshot summaries,
  feature scores, generated plans, planned orders and submitted orders.
- Current positions now include age, average cost, unrealized PnL and stale
  status in the app.
- Positions older than `CRYPTO_OFIM_MAX_HOLDING_SECONDS` are flagged; if their
  signal has disappeared, the next plan bypasses empty-signal delay and plans
  an exit.
- The app exposes recent feature logs and event logs under a collapsed raw-log
  section, so daily monitoring stays simple.
- A separate WebSocket market-data plug now maintains a local Binance order
  book and recent trade cache under `runtime/crypto_ofim/ws_cache.json`.
- Strategy cycles prefer the WebSocket cache for LOB/tick data and fall back to
  REST if the cache is missing or stale.
- The crypto watchdog also monitors the stream process and cache freshness, so
  `Guarded Start` now starts stream + auto trader + watchdog together.
- In Binance Testnet mode the same stream plug also attempts to listen to
  account fill events through Binance's newer signed WebSocket API account
  stream. When that stream is available, per-fill events are written to
  `runtime/crypto_ofim/user_fills.jsonl`.
- If the signed WebSocket API account stream is unavailable, the stream plug
  falls back to the old listen-key stream. Some Binance Testnet environments
  return `410 Gone` for that old endpoint; this is treated as a degraded
  account-stream state, not a fatal stream failure.
- The ledger prefers account-stream fills when available and skips duplicate
  aggregate order-log records for the same exchange order id. Older orders, and
  environments without account-stream fills, still fall back to
  `runtime/crypto_ofim/orders.jsonl`.

This is still a replay journal, not a low-latency matching engine. It makes
debugging and accounting clearer first.

## Still queued larger upgrade

The next large change should not be mixed with small parameter fixes. Queue it
as a separate branch:

- Score OFIM on event time, then downsample for the dashboard.
- Add a one-click testnet liquidation + ledger epoch reset flow so a new
  experiment can start from all-USDT cash without deleting old audit records.
- Keep REST polling as a watchdog/fallback path.

This is the proper path toward lower-latency OFIM. It is also a larger
architecture change, so it should be implemented and tested separately.
