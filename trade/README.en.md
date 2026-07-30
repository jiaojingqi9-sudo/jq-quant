# taa_futu — a personal quant trading workbench

> Reproducible, paper-tradable strategies for **Futu** (stocks) and **Binance**
> (crypto), on an auditable accounting + learning backbone.
> Simulation-first; real money is locked behind multiple explicit switches.
>
> 中文使用说明见 [`README.zh-CN.md`](README.zh-CN.md) · 架构详解见
> [`ARCHITECTURE.md`](ARCHITECTURE.md)

![python](https://img.shields.io/badge/python-3.11-blue)
![tests](https://img.shields.io/badge/tests-372%20passing-brightgreen)
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

## Quickstart

```bash
uv venv --python 3.11 .venv
uv pip install -p .venv/bin/python -e .[dev]
cp .env.example .env            # configure; SIMULATE by default

.venv/bin/taa-futu backtest                 # baseline monthly backtest
.venv/bin/taa-futu signals                  # latest completed-month target weights
.venv/bin/taa-futu paper-trade              # plan Futu orders (dry-run)
.venv/bin/taa-futu live-signal --symbol US.NVDA --json
.venv/bin/taa-futu dashboard                # local monitoring UI
.venv/bin/pytest tests/ -q                  # 372 passing (~4s, offline)
```

Running against a Futu account also requires a local **Futu OpenD** instance
(logged in, OpenAPI enabled). See [`ARCHITECTURE.md`](ARCHITECTURE.md) §5 for the
real-trading safety switches.

## Engineering highlights (for reviewers)

- `src/` layout, `uv.lock`-pinned deps, frozen-dataclass config with load-time
  validation, ~370 fast offline unit tests.
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
