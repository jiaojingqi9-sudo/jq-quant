# Market News Collector MVP

This repository contains a first runnable version of an abstract market news collector focused on low coupling, high cohesion, and later extensibility.

## Design goals

- Keep the core pipeline independent from any single data source.
- Isolate each capability behind a stable port: collect, normalize, deduplicate, cluster, assess impact, map instruments, rank, persist, report.
- Let later features be added as modules instead of rewriting the core.
- Serve global market news, but only rank A-share, Hong Kong, and US instruments.

## Current pipeline

```text
collect -> normalize -> deduplicate -> cluster -> impact assess -> instrument map -> rank -> persist -> report
```

## Current implementation

- `market_news/domain/`: stable domain models and ports.
- `market_news/application/`: orchestration logic.
- `market_news/services/`: default implementations for core business logic.
- `market_news/infrastructure/`: adapters for local JSON input, live HTTP fetches, and SQLite persistence.
- `market_news/infrastructure/collectors/rss.py`: generic RSS/Atom collector.
- `market_news/infrastructure/collectors/cninfo.py`: official CNINFO latest-announcements collector.
- `market_news/dashboard.py`: terminal dashboard renderer.
- `config/impact_rules.json`: config-driven event impact rules.
- `config/instrument_universe.json`: config-driven A/HK/US instrument universe.
- `config/live_sources.json`: first batch of official live sources.
- `data/sample_news.json`: local sample feed for the first end-to-end run.

## Run sample data

```bash
python3 -m market_news run
```

Generated outputs:

- `data/market_news.db`
- `reports/latest_report.json`
- `reports/latest_report.md`

## Run live authoritative sources

The first live source set is intentionally conservative and authority-first:

- CNINFO latest announcements for A-shares
- HKEX official news release RSS for Hong Kong
- SEC official press release RSS
- SEC official XBRL RSS feeds

```bash
python3 -m market_news live
```

Optional:

```bash
MARKET_NEWS_USER_AGENT="YourName your_email@example.com" python3 -m market_news live
```

Generated outputs:

- `data/market_news_live.db`
- `reports/live/latest_report.json`
- `reports/live/latest_report.md`

## Console dashboard

Render the latest live report:

```bash
python3 -m market_news dashboard
```

Fetch fresh live data, then render:

```bash
python3 -m market_news dashboard --refresh
```

Continuous watch mode:

```bash
python3 -m market_news dashboard --refresh --watch --interval 300
```

This will fetch new authoritative data every 5 minutes and redraw the console.

## Unified monitor mode

Run the full chain in one command:

```bash
python3 -m market_news monitor
```

Continuous end-to-end watch mode:

```bash
python3 -m market_news monitor --watch --interval 300
```

This mode will:

- fetch live authoritative sources
- rank events and instruments
- refresh the console dashboard
- send new high-priority alerts to your phone through OpenClaw

Useful options:

```bash
python3 -m market_news monitor --dry-run-notify
python3 -m market_news monitor --skip-notify
python3 -m market_news monitor --watch --interval 120 --max-alerts 2
```

Monitor artifacts:

- `reports/live/monitor_status.json`: latest end-to-end cycle health
- `reports/live/monitor_history.jsonl`: append-only cycle history
- `reports/live/latest_report.json`: latest structured market report
- `reports/live/latest_phone_alert.txt`: exact outbound mobile-alert preview

## Clickable web board

Every monitor cycle now also generates a clickable HTML board:

- `reports/live/latest_dashboard.html`

You can open it directly in your browser after any `monitor` or `live` run.

Recommended launcher:

- `scripts/market_news_board.command`

What the board supports:

- click alerts to jump to event detail
- click event cards to inspect rationale, related instruments, and source documents
- click instrument cards to jump back to the driving event
- click latest-feed items to open original source links
- search by headline, theme, company, or symbol
- direction filters for positive, negative, and neutral events
- auto refresh in the browser every 60 seconds

Recommended usage:

```bash
./scripts/market_news_board.command
```

## Phone alerts with OpenClaw

Generate a WhatsApp preview without sending:

```bash
python3 -m market_news notify --refresh --dry-run
```

Send new `high` and `critical` alerts to the first WhatsApp target found in `~/.openclaw/openclaw.json`:

```bash
python3 -m market_news notify --refresh
```

Useful options:

```bash
python3 -m market_news notify --refresh --max-alerts 2
python3 -m market_news notify --refresh --target +85259908875
python3 -m market_news notify --watch --refresh --interval 300
```

End-to-end phone-delivery test without waiting for a real new alert:

```bash
python3 -m market_news probe
```

Preview the synthetic test message only:

```bash
python3 -m market_news probe --dry-run
```

Artifacts:

- `reports/live/latest_phone_alert.txt`: the exact outbound mobile-alert preview
- `reports/live/latest_probe_message.txt`: the exact outbound synthetic test message
- `data/market_news_live.db`: also stores alert delivery history to avoid duplicate sends
- `scripts/market_news_console.command`: desktop launcher for the unified monitor mode

Note: OpenClaw's WhatsApp channel requires an active gateway listener. If sends fail with a gateway error, start it with:

```bash
~/.openclaw/bin/openclaw gateway
```

## Test

```bash
python3 -m unittest discover -s tests
```

## Extension points

- Replace `LocalJSONCollector` with RSS, API, webpage, or websocket collectors.
- Add more source types in `config/live_sources.json` without changing the pipeline core.
- Replace rule-based `ConfigDrivenImpactAnalyzer` with an ML or LLM-based assessor.
- Expand the instrument universe without touching the core pipeline.
- Replace `SQLiteRunStore` with PostgreSQL, Kafka, or another event store.
- Replace the reporter with REST, websocket push, dashboard, or automation outputs.
