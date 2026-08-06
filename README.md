# JQ Quant

A self-built quantitative trading and market-intelligence stack: strategy research
and backtesting, live market monitoring, simulated order routing against a real
broker API, and a news pipeline that turns raw headlines into ranked,
instrument-mapped signals — wrapped in one desktop application.

> **Simulation-first.** Real-money order paths sit behind several explicit
> switches. In demo mode every order path raises by design: it does not route to
> a fake account, it refuses to be called at all.

![python](https://img.shields.io/badge/python-3.11%20%E2%80%93%203.13-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![status](https://img.shields.io/badge/trading-simulation--first-orange)

*中文运维说明见 [`README.zh-CN.md`](README.zh-CN.md)。*

![Home](trade/docs/screenshots/01-%E9%A6%96%E9%A1%B5.png)

---

## Repo map

| Directory | What it is |
|---|---|
| [`trade/`](trade/) | **Start here.** The trading system: four equity sleeves, two crypto sleeves, a screener, and a Streamlit control terminal. Full write-up in [`trade/README.en.md`](trade/README.en.md); architecture in [`trade/ARCHITECTURE.md`](trade/ARCHITECTURE.md). |
| [`news-collector/`](news-collector/) | Market-news pipeline: collect → normalize → deduplicate → cluster → impact-score → instrument-map → rank → persist → push. Every capability sits behind a stable port, so no single data source is baked into the core. |
| [`watcher/`](watcher/) | Background file-queue service that runs read-only diagnostics and maintenance jobs on the host. Personal ops tooling. |

## The trading system at a glance

**Four equity sleeves**, combined into one account-level portfolio:

- *TAA Baseline* — Meb Faber 10-month moving-average tactical allocation
- *Fusion Intraday* — regime filter + opening-range breakout + momentum + VWAP + order-book/tick imbalance
- *OFIM Intraday* — order-flow-imbalance, L2-heavy; doubles as a top-N screener
- *Cascade* — bridges into a second, independent engine (`claude-trade`)

**Two crypto sleeves** — Binance spot OFIM and USD-M perpetual long/short, on paper
and testnet, with a research backtest and a locked-test parameter search.

**Accounting backbone** — event-sourced fills feed a double-entry journal secured by
a SHA-256 hash chain, reconciled against the broker, with a read-only
`stock-system-doctor` for integrity checks.

**Risk controls** — gross-exposure, per-symbol, per-order and per-cycle caps, plus an
epoch-loss brake.

**Learning lab** — records each decision, labels FIFO outcomes, attributes P&L,
proposes reversible candidate changes, gates promotion, and exports a SHA-256-stamped
review packet. Live auto-promotion is deliberately disabled: the loop proposes, a
human approves.

**Execution trainer** — a synthetic limit-order-book market to practise working
large orders in. Spread distribution, 20-level depth, traded volume, the intraday
volume curve and volatility clustering are all calibrated from order-book data
this repo's own logger collected, and checked against JP Morgan AI Research,
*Get Real: Realism Metrics for Robust Limit Order Book Market Simulations*
(arXiv 1912.04941). Scoring runs a **shadow market** — same seed, same window,
player absent — so market drift cancels out and only the participant's own
footprint (impact and information leakage) remains. Writeup:
[`trade/src/taa_futu/exec_trainer/README.md`](trade/src/taa_futu/exec_trainer/README.md).

## Engineering highlights (for reviewers)

- `src/` layout, `uv.lock`-pinned dependencies, frozen-dataclass configuration
  validated at load time.
- 464 test functions across 39 files in `trade/tests/`, plus 86 across 20 files in
  `news-collector/tests/` — all offline, no broker or network connection required.
- Event sourcing, double-entry bookkeeping and a tamper-evident hash chain:
  institutional accounting ideas applied to a personal account.
- Emergency cancellation is a standalone double-click script that does not depend on
  the app starting.
- Seven-screen desktop UI; screenshots in
  [`trade/docs/screenshots/`](trade/docs/screenshots/).

## Run it without a broker account

Demo mode drives the whole interface from synthetic data — five public ETFs,
fictional news events and obviously fake account numbers — so the entire app can be
clicked through with no Futu install and no account. Every order path raises by
design; it does not route to a fake account, it refuses to be called.

**Python must be 3.11, 3.12 or 3.13** (`requires-python = ">=3.11,<3.14"`).

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

Then open <http://localhost:8501>. Land directly on one screen with `?view=stock`,
`?view=news`, `?view=exec_trainer`, and so on.

Tests run fully offline, no broker and no network:

```bash
.venv/bin/python -m pytest -q      # 464 tests, ~27 min (the stock page renders end-to-end)
```

Longer setup notes, a synthetic-vs-real table and a troubleshooting section are in
[`trade/README.md`](trade/README.md) (Chinese) and
[`trade/README.en.md`](trade/README.en.md) (English).

## Safety boundaries

- Every order path raises in demo mode.
- The pre-trade gate has three settings: block, log-only, off.
- `.env`, `runtime/`, `*.db` and `*.log` are all git-ignored. No key, no real
  position and no account identifier is committed anywhere in this repository.
- Synthetic demo data is generated by a script that strips local machine paths.
- The learning lab only ever emits proposals; it never edits a live strategy.

---

*Educational and research software. Not investment advice.*
