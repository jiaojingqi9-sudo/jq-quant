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
- CSRC official website updates
- Xinhua homepage tech/finance stories
- Eastmoney finance portal stories
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

## Isolated runtime lines

The runtime is now split into separate lines so UI, delivery, and health can evolve independently:

- `collect`: fetch live sources, rank events, persist, and render reports
- `notify`: read the latest report and decide whether to push to your phone
- `health`: monitor heartbeat and artifact freshness of the active runtime lines
- `monitor`: legacy combined mode kept for compatibility

Run the collection line only:

```bash
python3 -m market_news collect
python3 -m market_news collect --watch --interval 300
```

Run the delivery line only:

```bash
python3 -m market_news notify
python3 -m market_news notify --watch --interval 300
```

Run the health line only:

```bash
python3 -m market_news health
python3 -m market_news health --watch --interval 60
```

Line artifacts:

- `reports/live/collect_status.json`
- `reports/live/collect_history.jsonl`
- `reports/live/delivery_status.json`
- `reports/live/delivery_history.jsonl`
- `reports/live/health_status.json`
- `reports/live/health_history.jsonl`

Unified stack launcher:

- `scripts/market_news_stack.command`
- `scripts/market_news_stack_stop.command`

Desktop shortcuts:

- `/Users/jiao/Desktop/市场新闻.command`

The stack launcher will:

- install or refresh three user `LaunchAgent` jobs
- schedule `collect` every 5 minutes
- schedule `notify` every 5 minutes
- schedule `health` every 60 seconds
- open the latest web board automatically
- write logs to `runtime/logs/`

Archived desktop helpers are kept in `runtime/desktop_archive/` and no longer clutter the desktop.

## Clickable web board

Every collection cycle now also generates a clickable HTML board:

- `reports/live/latest_dashboard.html`

You can open it directly in your browser after any `collect`, `monitor`, or `live` run.

Recommended launcher:

- `scripts/market_news_board.command`

This launcher now runs the isolated `collect` line only. It does not own phone delivery.

What the board supports:

- one-click view switching between the original global-market workspace and the A/H tech catalyst workspace
- runtime status cards for `collect`, `delivery`, and `health`
- module status chips for `core_market`, `tech_block`, and delivery-side notification modules
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

## AH tech catalyst block

The pipeline now ships with a dedicated feature block for speculative A-share and Hong Kong tech signals.

This block is intentionally isolated from the core ranking chain:

- it consumes the standard `PipelineSnapshot`
- it does not change the core event or instrument rankers
- it writes an additional `tech_block` payload into the report artifacts
- it renders a dedicated panel in the web board

The block is designed to catch "small but tradable" catalyst news rather than only the biggest macro headlines.

Its scoring model separates three ideas:

- `heat_score`: how hot and recent the event is
- `spec_score`: how likely the event is to become a short-term trading catalyst
- `importance_score`: how meaningful the event is beyond pure speculation

Final attention ranking:

```text
trading_attention_score = 0.40 * spec_score + 0.35 * heat_score + 0.25 * importance_score
```

Current inputs:

- `config/tech_universe_cn_hk.json`: tracked A-share and Hong Kong tech universe
- `config/tech_lexicon.json`: catalyst lexicon, speculative triggers, and negative-risk terms
- `config/tech_lexicon_release.json`: version, reviewer, and source trace for the active lexicon release
- `config/tech_impact_graph.json`: theme aliases and impact-chain propagation graph

Current outputs:

- `signals`: the top speculative tech event cards
- `themes`: the current hot-theme ladder
- `asset_ladder`: the strongest A/H tech candidates linked to those signals
- a dedicated web workspace you can switch to from the main board
- an optional notification section when a tech catalyst crosses the delivery threshold

Collection policy for this block:

- `Eastmoney`: API collector enabled by default for A/H announcements and fast news
- `Xinhua / 36Kr / Sina Tech / Huxiu / Ifeng Tech`: configured under `config/live_sources.json::rss.feeds`
- `Xinhua / Huxiu / Ifeng`: now wired as low-coupling official HTML list/detail collectors because the old public RSS URLs are no longer reliable
- `CSRC`: still wired as a low-coupling HTML collector for regulator updates
- `Weibo / Xueqiu`: cookie-backed social collectors require local cookies before you turn them on

This means the system now follows the design-spec collector split:

- Eastmoney comes from structured API endpoints
- 36Kr comes from live RSS
- Xinhua Tech, Huxiu Tech, Ifeng Tech, and CSRC come from public HTML collectors
- Weibo uses a cookie-backed collector with browser fallback for resilient API access
- Xueqiu uses a cookie-backed homepage timeline collector, then filters hot posts against your query list

Note:

- Sina Tech support remains in code, but it is not enabled in the default live config right now because the current public feed endpoints are stale rather than truly live.

Artifacts:

- `reports/latest_report.json`
- `reports/latest_report.md`
- `reports/latest_dashboard.html`
- `reports/live/latest_report.json`
- `reports/live/latest_dashboard.html`

In the web board, the `AH Tech Catalyst Block` panel lets you:

- scan the top catalyst signals
- inspect the matched catalyst terms and activated themes
- view the propagated impact-chain rationale
- jump from a signal back into the shared event-detail panel
- review the ranked A/H tech candidate ladder
- review the `Lexicon Discovery` queue for new pending theme terms

Lexicon maintenance commands:

```bash
python3 -m market_news lexicon feedback --signal-id <cluster_id> --result good
python3 -m market_news lexicon suggest
python3 -m market_news lexicon bump --apply --reviewer jiao
python3 -m market_news lexicon discover --limit 20
python3 -m market_news lexicon add <词条> --type theme
python3 -m market_news lexicon reject <词条>
```

Unknown-term discovery now runs automatically after each `run` / `collect` cycle and appends pending candidates to:

- `data/lexicon_discovery.jsonl`

`market_news lexicon discover` also auto-prunes obvious legal/boilerplate noise before listing the pending queue.

Detector config lives here:

- `config/tech_block.json`

Cookie maintenance commands:

```bash
python3 -m market_news cookies set-weibo --cookie-file /path/to/weibo_cookies.json
python3 -m market_news cookies set-xueqiu --cookie-file /path/to/xueqiu_cookies.json
python3 -m market_news cookies check
```

Cookie templates live here:

- `config/examples/weibo_cookies.example.json`
- `config/examples/xueqiu_cookies.example.json`

Recommended social-source bring-up flow:

```bash
python3 -m market_news cookies set-weibo --cookie-file /path/to/weibo_cookies.json
python3 -m market_news cookies set-xueqiu --cookie-file /path/to/xueqiu_cookies.json
python3 -m market_news cookies check
```

After `cookies check` shows both sources as healthy, make sure `config/live_sources.json` has:

- set `weibo.enabled` to `true`
- set `xueqiu.enabled` to `true`

Then rerun:

```bash
python3 -m market_news collect
python3 -m market_news notify --dry-run --include-existing --force
```

## Phone alerts with OpenClaw

Generate a WhatsApp preview without sending:

```bash
python3 -m market_news notify --dry-run
```

Send new `high` and `critical` alerts to the first WhatsApp target found in `~/.openclaw/openclaw.json`:

```bash
python3 -m market_news notify
```

Useful options:

```bash
python3 -m market_news notify --refresh --max-alerts 2
python3 -m market_news notify --target +85259908875
python3 -m market_news notify --watch --interval 300
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
- `scripts/market_news_delivery.command`: desktop launcher for the isolated delivery line

## Health monitor

The health monitor is intentionally low-coupling: it only reads status JSON files and output artifacts.

Default behavior:

- if `collect_status.json` and `delivery_status.json` exist, it checks those
- otherwise it falls back to the legacy `monitor_status.json`
- it marks a line stale if the heartbeat is older than `--max-age`
- it marks a line degraded if the upstream status is not healthy or the report artifact is missing

Useful options:

```bash
python3 -m market_news health --max-age 900
python3 -m market_news health --status reports/live/collect_status.json --status reports/live/delivery_status.json
python3 -m market_news health --watch --interval 60
```

Desktop launcher:

- `scripts/market_news_health.command`

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
