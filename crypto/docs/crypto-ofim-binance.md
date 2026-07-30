# Crypto OFIM Binance Plug

This is an independent crypto plug. It does not route through Futu, does not
share the Futu strategy stack, and keeps its own runtime files under
`runtime/crypto_ofim/`.

Key runtime files:

- `runtime/crypto_ofim/status.json`: latest cycle status shown in the app.
- `runtime/crypto_ofim/orders.jsonl`: submitted/filled/rejected order records.
- `runtime/crypto_ofim/features.jsonl`: recent OFIM feature scores.
- `runtime/crypto_ofim/events.jsonl`: replayable cycle event journal.
- `runtime/crypto_ofim/ws_cache.json`: latest WebSocket order-book/trade cache.
- `runtime/crypto_ofim/stream_status.json`: market-stream plug health.
- `runtime/crypto_ofim/user_stream_events.jsonl`: Binance account stream events.
- `runtime/crypto_ofim/user_fills.jsonl`: per-fill exchange events used by the ledger.
- `~/Library/LaunchAgents/com.jiao.taa_futu_crypto_ofim_watchdog.plist`: macOS service used by
  `Guarded Start` so the watchdog survives Terminal/Streamlit/browser restarts.

## Modes

- `paper`: local simulated ledger using Binance public market data. No API key.
- `testnet`: Binance Spot Test Network. Uses a Spot Testnet API key and secret.
- `live`: intentionally not supported in this plug.

## One-App Visual Console

Use this first. It keeps controls and monitoring on one page:

```bash
.venv/bin/python -m taa_futu.cli crypto-ofim-app --port 8503
```

Or double-click the macOS app:

```text
~/Desktop/Crypto OFIM Binance.app
```

## Local Paper Setup

Default settings already run in safe paper mode:

```bash
.venv/bin/python -m taa_futu.cli crypto-ofim-check
.venv/bin/python -m taa_futu.cli crypto-ofim-once
```

To let the paper ledger actually change:

```bash
.venv/bin/python -m taa_futu.cli crypto-ofim-once --submit
```

To run continuously:

```bash
.venv/bin/python -m taa_futu.cli crypto-ofim-auto --submit --poll-seconds 15
```

In the visual app, use `一键守护运行 / Guarded Start` for the normal workflow.
It starts the WebSocket stream, auto trader, and a macOS LaunchAgent watchdog. If the app page
or Terminal window is closed, the watchdog keeps checking the auto trader and restarts it when
the status file becomes stale.
That starts three isolated plugs together: WebSocket market/account stream, auto
trader and watchdog. The strategy consumes the local stream cache first and only
falls back to REST when the cache is stale or missing. In Binance Testnet mode,
the same stream plug also attempts to listen to account fill events through
Binance's signed WebSocket API account stream. If that is unavailable, it tries
the legacy listen-key stream. If both account streams are unavailable, the
market stream stays alive and the ledger falls back to aggregate order-response
records instead of killing the bot.

To reset the local paper ledger:

```bash
.venv/bin/python -m taa_futu.cli crypto-ofim-reset
```

For Binance Spot Testnet, the safe fresh-start flow is:

1. Stop auto trading.
2. Sell all free non-USDT testnet assets into USDT.
3. Set a new ledger epoch so new PnL starts from that point while old audit
   files stay on disk.

In the app this is hidden under `诊断与高级操作 / Diagnostics & Advanced` →
`危险操作 / Dangerous` → `全换 USDT + 重开账本 / Sweep + New Ledger`.

CLI equivalent:

```bash
.venv/bin/python -m taa_futu.cli crypto-ofim-liquidate --submit --reset-epoch
```

## `.env` Settings

```bash
CRYPTO_OFIM_MODE=paper
CRYPTO_OFIM_SYMBOLS=TIGHT_USDT
CRYPTO_OFIM_HOT_UNIVERSE=false
CRYPTO_OFIM_CORE_UNIVERSE=false
CRYPTO_OFIM_AUTO_POLL_SECONDS=15
CRYPTO_OFIM_HOT_COUNT=5
CRYPTO_OFIM_BENCHMARK=BTCUSDT
CRYPTO_OFIM_INITIAL_CASH=10000
CRYPTO_OFIM_ENTRY_THRESHOLD=0.44
CRYPTO_OFIM_EXIT_THRESHOLD=0.10
CRYPTO_OFIM_MIN_VOL_ACCELERATION=1.05
CRYPTO_OFIM_FEE_RATE=
CRYPTO_OFIM_SLIPPAGE_BPS=5
CRYPTO_OFIM_DEPTH_LIMIT=100
CRYPTO_OFIM_MAX_SPREAD_BPS=10.24
CRYPTO_OFIM_USE_WS_CACHE=true
CRYPTO_OFIM_USE_USER_STREAM=true
CRYPTO_OFIM_MAX_POSITIONS=1
CRYPTO_OFIM_MAX_POSITION_WEIGHT=0.50
CRYPTO_OFIM_MIN_ORDER_NOTIONAL=67.5
CRYPTO_OFIM_REBALANCE_THRESHOLD=0.05
CRYPTO_OFIM_EXIT_CONFIRM_CYCLES=2
CRYPTO_OFIM_MIN_TRADE_INTERVAL_SECONDS=600
CRYPTO_OFIM_MIN_REENTRY_AFTER_RISK_OFF_SECONDS=57600
CRYPTO_OFIM_MIN_HOLDING_SECONDS=300
CRYPTO_OFIM_MAX_HOLDING_SECONDS=600
```

`CRYPTO_OFIM_USE_WS_CACHE=true` means the strategy normally reads order book and
recent trades from the local WebSocket stream cache. `CRYPTO_OFIM_DEPTH_LIMIT=100`
means the stream keeps the top 100 bid/ask levels. That is enough for the
current OFIM features, which use tiers 1-5, 6-20 and 21-60.

If the stream is not running, stale, or reconnecting, the strategy falls back to
REST snapshots/trades for that cycle. The watchdog will also try to restart a
broken stream.

`CRYPTO_OFIM_USE_USER_STREAM=true` means the app tries to capture Binance
Testnet fill events into `user_fills.jsonl`. It uses Binance's newer signed
WebSocket API account stream first, then falls back to the legacy listen-key
stream if needed. When account-stream fill events are available, the ledger uses
them first and skips duplicate aggregate order records for the same Binance
order id. If the account stream is unavailable, the app shows that state
explicitly and continues using `orders.jsonl` as the ledger fallback.

The app also shows current position age. If a position is older than
`CRYPTO_OFIM_MAX_HOLDING_SECONDS` and the signal has disappeared, the next
cycle skips the normal empty-signal delay and plans an exit. This is the
anti-stuck-position guard.

`CRYPTO_OFIM_MIN_HOLDING_SECONDS` blocks routine rebalance sells before the
position has aged enough. It does not block risk-off or loss-guard exits. The
default is 300 seconds to reduce sub-5-minute round trips where spot fees can
dominate expected edge.

The default `TIGHT_USDT` universe is intentionally tighter: BTC, ETH, SOL, BNB
and XRP. This keeps the bot focused on high-liquidity USDT pairs and lowers API
load/noise for faster OFIM testing. `CORE_USDT` remains available if you want
the broader 10-coin pool: BTC, ETH, BNB, SOL, XRP, DOGE, ADA, LINK, AVAX and
LTC.

## Binance Spot Testnet Setup

1. Open Binance Spot Test Network: <https://testnet.binance.vision/>
2. Sign in with your Binance account.
3. On the API Keys page, click `Generate HMAC-SHA-256 Key`.
4. If you already have a key, click `Edit` and confirm the permissions include
   `TRADE`, `USER_DATA`, and `USER_STREAM`.
5. Copy the API Key and Secret into the visual app settings panel, or put them
   in local `.env`.
6. Do not paste API secrets into chat.

```bash
CRYPTO_OFIM_MODE=testnet
CRYPTO_OFIM_API_KEY=your_testnet_key
CRYPTO_OFIM_API_SECRET=your_testnet_secret
```

Then run:

```bash
.venv/bin/python -m taa_futu.cli crypto-ofim-check
.venv/bin/python -m taa_futu.cli crypto-ofim-once
.venv/bin/python -m taa_futu.cli crypto-ofim-once --submit
```

## Safety Notes

- Keep withdrawals disabled. This plug does not need withdrawal permission.
- For live keys later, only enable read/account data and spot trading. Never
  enable withdrawal for a trading bot.
- Start with `paper`. Move to `testnet` only after paper behavior makes sense.
- Testnet keys are not live trading keys. Do not reuse live API keys here.

## Why No Withdrawal Permission

Trading only needs market data, account/order status and order placement. A
withdrawal permission is a different risk surface: if a key leaks, an attacker
could try to move assets out of the exchange. The strategy never needs to move
coins to an external wallet, so withdrawal permission should stay off.
