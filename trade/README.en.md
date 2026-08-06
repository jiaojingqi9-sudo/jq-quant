# taa_futu — a personal quant trading workbench

> Reproducible, paper-tradable strategies for **Futu** (stocks) and **Binance**
> (crypto), on an auditable accounting + learning backbone.
> Simulation-first; real money is locked behind multiple explicit switches.
>
> 中文使用说明见 [`README.md`](README.md) · 架构详解见
> [`ARCHITECTURE.md`](ARCHITECTURE.md)

![python](https://img.shields.io/badge/python-3.11%20%E2%80%93%203.13-blue)
![tests](https://img.shields.io/badge/tests-461-brightgreen)
![license](https://img.shields.io/badge/license-MIT-green)
![status](https://img.shields.io/badge/trading-simulation--first-orange)

## Why it exists

Most public "quant" repos chase the prettiest backtest. This one starts from a
published, verifiable strategy paper and builds the boring-but-real parts around
it: execution against a live broker API, transaction-cost realism, a
tamper-evident ledger, pre-trade risk limits, and an evidence-driven learning
loop that proposes improvements **without** silently pushing them to live money.

## What's inside

- **Four stock sleeves**, combined into one account-level portfolio:
  - *TAA Baseline* — Meb Faber 10-month moving-average tactical allocation
  - *Fusion Intraday* — event-driven: regime filter + opening-range breakout +
    momentum + VWAP + order-book/tick imbalance
  - *OFIM Intraday* — order-flow-imbalance, L2-heavy (also a top-N screener)
  - *Cascade* — bridges into a second, independent engine (`claude_trade`)
- **Crypto sleeves** — Binance spot OFIM + USD-M perpetual long/short
  (paper / testnet), with a research backtest + locked-test parameter search.
- **Accounting backbone** — event-sourced fills → double-entry journal with a
  **hash chain**, broker reconciliation, and a read-only `stock-system-doctor`.
- **Risk controls** — gross-exposure / per-symbol / per-order / per-cycle caps
  and an epoch-loss brake.
- **Learning lab** — records decisions, labels FIFO outcomes, attributes P&L,
  proposes reversible candidates, gates promotion, and exports a SHA-256-stamped
  review packet. Live auto-promotion is intentionally disabled.
- **Execution trainer** — a synthetic limit-order-book market you practise large
  orders against. Spread distribution, 20-level depth, volume, intraday curve and
  volatility clustering are calibrated from order-book data collected by this
  repo's own logger, and validated against JP Morgan AI Research,
  *Get Real: Realism Metrics for Robust Limit Order Book Market Simulations*
  (arXiv 1912.04941). Scoring is against a **shadow market** — the same seed, the
  same window, run again with the player absent — so price drift cancels and what
  is left is only the participant's own footprint. Details in
  [`src/taa_futu/exec_trainer/README.md`](src/taa_futu/exec_trainer/README.md)
  (Chinese).

## Demo mode — click through the whole app with no broker account

No Futu install, no account, no market-data entitlement. Every order path raises
by design.

**Python must be 3.11, 3.12 or 3.13** (`requires-python = ">=3.11,<3.14"`).
Below 3.11 the code fails at import on `datetime.UTC`; 3.14 is refused by pip.

macOS / Linux:

```bash
git clone https://github.com/jiaojingqi9-sudo/jq-quant.git
cd jq-quant/trade

python3 -m venv .venv
.venv/bin/pip install -e .

JQ_DEMO=1 JQ_NEWS_ROOT="$PWD/demo_data/news" \
  .venv/bin/python -m streamlit run src/taa_futu/dashboard_app.py
```

Windows (cmd) — executables live in `.venv\Scripts\`, and environment variables
cannot be prefixed to the command:

```bat
git clone https://github.com/jiaojingqi9-sudo/jq-quant.git
cd jq-quant\trade

python -m venv .venv
.venv\Scripts\pip install -e .

set JQ_DEMO=1
set JQ_NEWS_ROOT=%CD%\demo_data\news
.venv\Scripts\python -m streamlit run src\taa_futu\dashboard_app.py
```

Then open <http://localhost:8501>. Jump straight to one screen with
`?view=stock`, `?view=crypto`, `?view=screener`, `?view=live_signal`,
`?view=news`, `?view=stock_history`, `?view=exec_trainer`.

The Chinese [`README.md`](README.md) has the full demo-mode table (what is
synthetic vs. real) and a troubleshooting section.

## Quickstart (CLI)

```bash
cp .env.example .env            # configure; SIMULATE by default

.venv/bin/taa-futu backtest                 # baseline monthly backtest
.venv/bin/taa-futu signals                  # latest completed-month target weights
.venv/bin/taa-futu paper-trade              # plan Futu orders (dry-run)
.venv/bin/taa-futu live-signal --symbol US.NVDA --json
.venv/bin/taa-futu dashboard                # local monitoring UI
.venv/bin/taa-futu stock-system-doctor      # read-only integrity check
```

`uv` works too if you prefer it: `uv venv --python 3.13 .venv` then
`uv pip install -p .venv/bin/python -e ".[dev]"` (quote the extras — zsh globs
the brackets otherwise).

Tests are fully offline — no broker, no network:

```bash
.venv/bin/python -m pytest -q               # 461 tests, ~27 min
.venv/bin/python -m pytest tests/test_dashboard_extras.py -q   # fast subset
```

The full run is slow because the stock-page end-to-end tests render the entire
Streamlit UI.

Running against a Futu account also requires a local **Futu OpenD** instance
(logged in, OpenAPI enabled). See [`ARCHITECTURE.md`](ARCHITECTURE.md) §5 for the
real-trading safety switches.

## Engineering highlights (for reviewers)

- `src/` layout, `uv.lock`-pinned deps, frozen-dataclass config with load-time
  validation, 461 offline tests across 39 files.
- Event sourcing + double-entry + tamper-evident hash chain — institutional
  accounting ideas applied to a hobby account.
- Human-in-the-loop strategy evolution: the learning loop can *propose* but never
  *apply* live changes without manual approval.

## Repo map

See [`ARCHITECTURE.md`](ARCHITECTURE.md) §6. The mature engine is `src/taa_futu/`;
`claude-trade/` is a second engine reached only through the Cascade sleeve;
`../news collector/` and `../futu_watcher/` are sibling services.

---

*Educational / research software. Not investment advice.*
