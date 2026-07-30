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
- `scripts/market_news_learning_auto.command`
- `scripts/market_news_learning_auto_stop.command`
- `scripts/market_news_codex_review_auto.command`
- `scripts/market_news_codex_review_auto_stop.command`
- `scripts/market_news_thread_review_auto.command`
- `scripts/market_news_thread_review_auto_stop.command`

Desktop shortcuts:

- `/Users/jiao/Desktop/市场新闻.command`

The stack launcher will:

- install or refresh user `LaunchAgent` jobs for collection, notification, health, news learning, and review API
- schedule `collect` every 5 minutes
- schedule `notify` every 5 minutes
- schedule `health` every 60 seconds
- schedule `news-learning-auto` every 5 minutes
- open the latest web board automatically
- write logs to `runtime/logs/`

If you only want the Evidence-to-Review learning loop, without restarting collection or notification:

```bash
./scripts/market_news_learning_auto.command
```

Stop only that learning loop:

```bash
./scripts/market_news_learning_auto_stop.command
```

If you want Codex itself to review the generated learning packet hourly and send actionable findings to your phone:

```bash
./scripts/market_news_codex_review_auto.command
```

Stop only that Codex-review automation:

```bash
./scripts/market_news_codex_review_auto_stop.command
```

The Codex-review automation runs read-only and writes:

- `reports/news_learning/news_learning_codex_analysis.md`
- `reports/live/news_learning_codex_review_status.json`
- `reports/live/news_learning_codex_review_history.jsonl`

If you want the review to come back into this existing Codex chat thread instead of going to your phone:

```bash
./scripts/market_news_thread_review_auto.command
```

Stop that thread-based review:

```bash
./scripts/market_news_thread_review_auto_stop.command
```

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

## Model backends (Claude CLI / OpenAI / OpenClaw)

The judgement layer tries backends in order and uses the first one that answers:

```text
OpenAI HTTP (needs OPENAI_API_KEY)  ->  Claude Code CLI (local)  ->  OpenClaw/Codex
```

Claude Code CLI is the current working backend. It shells out to the locally
installed `claude` binary in non-interactive mode (`claude -p`), so it needs no
API key and no network credentials of its own.

```json
"claude_enabled": true,
"claude_bin": "/opt/homebrew/bin/claude",
"claude_model": "",
"claude_timeout": 180,
"claude_max_screening_calls_per_run": 8,
"claude_max_asset_calls_per_run": 3
```

Two things to know:

- **Use an absolute path for `claude_bin`.** launchd jobs run with
  `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, which does not include Homebrew, so a bare
  `claude` resolves fine in your shell but not under launchd.
- **Judgement caches are namespaced per backend** (`claude-screen`, `openclaw-screen`, …),
  so verdicts from different models never share a cache slot.

`openclaw_enabled` is currently `false`: that path is a Codex subscription that
exhausted its quota, and every attempt still consumed a slot from the shared daily
budget, so leaving it enabled starved the working backend.

### Delivery must not depend on the model being alive

Alert delivery used to require a full model verdict
(`screening_status == "used"`) for every alert. When the model layer went down,
that gate silently dropped **100%** of alerts — the pipeline kept collecting and
ranking news, and nothing reached the phone.

Delivery now fails open, but at a stricter bar:

- if at least one event in the report carries a real verdict, the model layer is
  considered alive and its verdicts stay authoritative — including its rejections
- if **no** event carries a verdict, the layer is treated as down: alerts are
  gated by rules at `critical`/`high` only, and the message is tagged
  `⚙️ AI 筛选层不可用，本条按规则筛选（仅高危级）`
- A/H tech-catalyst signals are muted in the degraded path, because separating a
  real catalyst from boilerplate is exactly what the rules cannot do alone

Regression tests: `tests/test_notifications.py::test_digest_fails_open_when_model_layer_never_ran`
and `::test_degraded_mode_still_respects_level_bar`.

## GPT judgement layer

The rule/lexicon system is now wrapped by an optional GPT judgement layer.

It is designed as a plug-in layer rather than a replacement for the core pipeline:

- the original rule engine still runs first
- the default focus is fundamental: earnings, revenue, cash flow, margin, ROE, durable demand, and valuation repair
- GPT screening then decides whether a clean-source fundamental event is worth attention
- GPT asset mapping only runs after screening and must explain the stock impact chain
- Weibo and Xueqiu remain heat sources only; they cannot independently create a tradable event
- regulatory investigations, penalties, abnormal trading, pledges, and reductions are kept as risk memos; they are not allowed to crowd out earnings or valuation work
- if `OPENAI_API_KEY` is not configured, the system safely falls back to the existing rules
- if OpenClaw is logged in, the system can optionally use `openclaw agent --session-id market-news-judge --json` as a fallback model backend

Config:

- `config/model_judgement.json`

Environment:

```bash
export OPENAI_API_KEY="sk-..."
export MARKET_NEWS_MODEL_JUDGE=1
export MARKET_NEWS_SCREENING_MODEL="gpt-4.1-mini"
export MARKET_NEWS_ASSET_MODEL="gpt-4.1"
```

For the desktop/launchd stack, put the key in a local private env file instead:

```bash
mkdir -p ~/.market_news
printf 'export OPENAI_API_KEY="sk-..."\n' > ~/.market_news/openai_env
chmod 600 ~/.market_news/openai_env
```

Operational split:

- screening model: cheaper/fast model for "is this news worth attention?"
- asset model: stronger model for "which A/H/US instruments could move, and why?"
- attention gate: model calls require a simple fundamental or durable-demand chain; phone alerts require an even higher "actionability" gate
- attention gate adds weight for quantified earnings/cash-flow/margin evidence, valuation terms, durable policy demand, high-trust sources, and direct company evidence
- attention gate subtracts weight for low-predictability risk events, routine exchange plumbing, after-the-fact price reaction headlines, and news that cannot support valuation work
- leading signals such as customer loss, order cancellation, purchase termination, backlog decline, or contract termination are promoted because they can appear before the price reaction
- cache: `data/model_judgement_cache.json`, so repeated dashboard refreshes do not keep asking the same question
- news judgement budget: `data/model_judgement_budget.json`; default `model_daily_call_limit` is `30`
- OpenClaw fallback: enabled in `config/model_judgement.json`, capped at `4` screening calls per collection cycle and `1` asset-mapping call per cycle by default
- lexicon/manual review budget: `data/review_api_model_budget.json`; default `MARKET_NEWS_MANUAL_AI_DAILY_LIMIT` is `12`, separated from news judgement so vocabulary work cannot crowd out important news
- emergency off switch: set `MARKET_NEWS_MODEL_JUDGE_DISABLED=1` before starting the stack

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
