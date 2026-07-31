from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

from market_news.application.health import (
    HealthStateWriter,
    discover_status_files,
    evaluate_status_files,
    exit_code_for,
    render_health_screen,
)
from market_news.application.monitoring import DeliveryStateWriter, MonitorStateWriter
from market_news.application.notify import NotificationResult, NotificationRunner
from market_news.application.pipeline import MarketNewsPipeline
from market_news.application.review_api import (
    LexiconReviewService,
    OpenClawReviewAssistant,
    ReviewApiStateWriter,
    serve_review_api,
)
from market_news.common import utcnow
from market_news.dashboard import render_dashboard, watch_dashboard
from market_news.domain.models import AlertLevel
from market_news.infrastructure.collectors.factory import build_live_collector
from market_news.infrastructure.collectors.weibo import WeiboCollector
from market_news.infrastructure.collectors.xueqiu import XueqiuCollector
from market_news.infrastructure.collectors.local_json import LocalJSONCollector
from market_news.infrastructure.cookie_store import (install_cookie_file, market_news_cookie_dir,
                                                     record_cookie_check, resolve_cookie_path)
from market_news.infrastructure.http import UrllibHttpClient, default_user_agent
from market_news.infrastructure.notifications.openclaw import OpenClawNotifier
from market_news.infrastructure.persistence.sqlite_store import SQLiteRunStore
from market_news.services.alerts import RuleBasedAlertEngine
from market_news.services.clustering import KeywordEventClusterer
from market_news.services.cooccurrence import CooccurrenceMiner
from market_news.services.deduplication import FingerprintDeduplicator
from market_news.services.impact import ConfigDrivenImpactAnalyzer
from market_news.services.lexicon_feedback import LexiconFeedbackStore
from market_news.services.lexicon_suggester import LexiconSuggester
from market_news.services.mapping import ConfigDrivenInstrumentMapper
from market_news.services.model_judgement import build_model_judgement_stack
from market_news.services.news_learning import build_news_learning_artifacts
from market_news.services.normalization import DefaultNormalizer
from market_news.services.ranking import WeightedEventRanker, WeightedInstrumentRanker
from market_news.services.reporting import MarkdownJsonReporter, refresh_runtime_status_views
from market_news.services.tech_block import AHShareTechFeatureBlock
from market_news.services.unknown_term_detector import UnknownTermDetector


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_live_report_path() -> Path:
    return project_root() / "reports" / "live" / "latest_report.json"


def default_phone_preview_path() -> Path:
    return project_root() / "reports" / "live" / "latest_phone_alert.txt"


def default_probe_preview_path() -> Path:
    return project_root() / "reports" / "live" / "latest_probe_message.txt"


def default_monitor_status_path() -> Path:
    return project_root() / "reports" / "live" / "monitor_status.json"


def default_monitor_history_path() -> Path:
    return project_root() / "reports" / "live" / "monitor_history.jsonl"


def default_collect_status_path() -> Path:
    return project_root() / "reports" / "live" / "collect_status.json"


def default_collect_history_path() -> Path:
    return project_root() / "reports" / "live" / "collect_history.jsonl"


def default_delivery_status_path() -> Path:
    return project_root() / "reports" / "live" / "delivery_status.json"


def default_delivery_history_path() -> Path:
    return project_root() / "reports" / "live" / "delivery_history.jsonl"


def default_health_status_path() -> Path:
    return project_root() / "reports" / "live" / "health_status.json"


def default_health_history_path() -> Path:
    return project_root() / "reports" / "live" / "health_history.jsonl"


def default_news_learning_status_path() -> Path:
    return project_root() / "reports" / "live" / "news_learning_status.json"


def default_news_learning_history_path() -> Path:
    return project_root() / "reports" / "live" / "news_learning_history.jsonl"


def default_news_learning_codex_review_status_path() -> Path:
    return project_root() / "reports" / "live" / "news_learning_codex_review_status.json"


def default_news_learning_codex_review_history_path() -> Path:
    return project_root() / "reports" / "live" / "news_learning_codex_review_history.jsonl"


def default_news_learning_codex_analysis_path() -> Path:
    return project_root() / "reports" / "news_learning" / "news_learning_codex_analysis.md"


def default_review_api_status_path() -> Path:
    return project_root() / "reports" / "live" / "review_api_status.json"


def default_review_api_history_path() -> Path:
    return project_root() / "reports" / "live" / "review_api_history.jsonl"


def default_lexicon_feedback_path() -> Path:
    return project_root() / "data" / "lexicon_feedback.jsonl"


def default_lexicon_suggestions_path() -> Path:
    return project_root() / "reports" / "live" / "lexicon_suggestions.json"


def default_cooccurrence_path() -> Path:
    return project_root() / "data" / "cooccurrence_candidates.jsonl"


def default_lexicon_discovery_path() -> Path:
    return project_root() / "data" / "lexicon_discovery.jsonl"


def default_tech_block_config_path() -> Path:
    return project_root() / "config" / "tech_block.json"


def default_model_judgement_config_path() -> Path:
    return project_root() / "config" / "model_judgement.json"


def default_news_learning_dir() -> Path:
    return project_root() / "reports" / "news_learning"


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="market-news",
        description="Run the abstract market news collector MVP pipeline.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run the end-to-end local sample pipeline.")
    run_parser.add_argument(
        "--input",
        type=Path,
        default=project_root() / "data" / "sample_news.json",
        help="Path to a local JSON file containing raw news records.",
    )
    run_parser.add_argument(
        "--rules",
        type=Path,
        default=project_root() / "config" / "impact_rules.json",
        help="Impact rule configuration path.",
    )
    run_parser.add_argument(
        "--universe",
        type=Path,
        default=project_root() / "config" / "instrument_universe.json",
        help="Instrument universe configuration path.",
    )
    run_parser.add_argument(
        "--database",
        type=Path,
        default=project_root() / "data" / "market_news.db",
        help="SQLite database path for run persistence.",
    )
    run_parser.add_argument(
        "--report-dir",
        type=Path,
        default=project_root() / "reports",
        help="Directory for generated markdown/json reports.",
    )
    run_parser.add_argument(
        "--top",
        type=int,
        default=8,
        help="Maximum number of top events and instruments in generated reports.",
    )

    live_parser = subparsers.add_parser("live", help="Fetch live authoritative sources and run the pipeline.")
    live_parser.add_argument(
        "--sources",
        type=Path,
        default=project_root() / "config" / "live_sources.json",
        help="Path to the live source configuration file.",
    )
    live_parser.add_argument(
        "--rules",
        type=Path,
        default=project_root() / "config" / "impact_rules.json",
        help="Impact rule configuration path.",
    )
    live_parser.add_argument(
        "--universe",
        type=Path,
        default=project_root() / "config" / "instrument_universe.json",
        help="Instrument universe configuration path.",
    )
    live_parser.add_argument(
        "--database",
        type=Path,
        default=project_root() / "data" / "market_news_live.db",
        help="SQLite database path for live run persistence.",
    )
    live_parser.add_argument(
        "--report-dir",
        type=Path,
        default=project_root() / "reports" / "live",
        help="Directory for generated live markdown/json reports.",
    )
    live_parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Maximum number of top events and instruments in generated reports.",
    )
    live_parser.add_argument(
        "--user-agent",
        default=os.environ.get("MARKET_NEWS_USER_AGENT", default_user_agent()),
        help="HTTP User-Agent for live source fetching.",
    )
    live_parser.add_argument(
        "--skip-news-learning",
        action="store_true",
        help="Do not generate news Evidence-to-Review artifacts after this live run.",
    )
    live_parser.add_argument(
        "--news-learning-dir",
        type=Path,
        default=default_news_learning_dir(),
        help="Directory for automatic news Evidence-to-Review artifacts.",
    )

    collect_parser = subparsers.add_parser(
        "collect",
        help="Run the collection line only: fetch, rank, persist, and render reports.",
    )
    collect_parser.add_argument(
        "--sources",
        type=Path,
        default=project_root() / "config" / "live_sources.json",
        help="Path to the live source configuration file.",
    )
    collect_parser.add_argument(
        "--rules",
        type=Path,
        default=project_root() / "config" / "impact_rules.json",
        help="Impact rule configuration path.",
    )
    collect_parser.add_argument(
        "--universe",
        type=Path,
        default=project_root() / "config" / "instrument_universe.json",
        help="Instrument universe configuration path.",
    )
    collect_parser.add_argument(
        "--database",
        type=Path,
        default=project_root() / "data" / "market_news_live.db",
        help="SQLite database path for live run persistence.",
    )
    collect_parser.add_argument(
        "--report-dir",
        type=Path,
        default=project_root() / "reports" / "live",
        help="Directory for generated live markdown/json reports.",
    )
    collect_parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Maximum number of ranked events and instruments.",
    )
    collect_parser.add_argument(
        "--user-agent",
        default=os.environ.get("MARKET_NEWS_USER_AGENT", default_user_agent()),
        help="HTTP User-Agent for live source fetching.",
    )
    collect_parser.add_argument(
        "--status-file",
        type=Path,
        default=default_collect_status_path(),
        help="Path to the latest collection-line status JSON file.",
    )
    collect_parser.add_argument(
        "--history-file",
        type=Path,
        default=default_collect_history_path(),
        help="Path to the append-only collection-line history JSONL file.",
    )
    collect_parser.add_argument(
        "--preview",
        type=Path,
        default=default_phone_preview_path(),
        help="Preview path recorded with collection status for downstream tooling.",
    )
    collect_parser.add_argument(
        "--watch",
        action="store_true",
        help="Continuously refresh the collection line.",
    )
    collect_parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Refresh interval in seconds when using --watch.",
    )
    collect_parser.add_argument(
        "--auto-repair",
        action="store_true",
        default=_env_flag("MARKET_NEWS_AUTO_REPAIR"),
        help="Retry an empty live collection once before marking the cycle degraded.",
    )
    collect_parser.add_argument(
        "--repair-delay",
        type=int,
        default=20,
        help="Seconds to wait before retrying an empty live collection when auto repair is enabled.",
    )
    collect_parser.add_argument(
        "--skip-news-learning",
        action="store_true",
        help="Do not generate news Evidence-to-Review artifacts after each collection cycle.",
    )
    collect_parser.add_argument(
        "--news-learning-dir",
        type=Path,
        default=default_news_learning_dir(),
        help="Directory for automatic news Evidence-to-Review artifacts.",
    )

    dashboard_parser = subparsers.add_parser("dashboard", help="Render the latest console dashboard.")
    dashboard_parser.add_argument(
        "--report",
        type=Path,
        default=default_live_report_path(),
        help="Path to a generated report JSON file.",
    )
    dashboard_parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Maximum number of events and instruments to render.",
    )
    dashboard_parser.add_argument(
        "--watch",
        action="store_true",
        help="Refresh the dashboard continuously.",
    )
    dashboard_parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Refresh interval in seconds when using --watch.",
    )
    dashboard_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Fetch fresh live data before rendering the dashboard.",
    )
    dashboard_parser.add_argument(
        "--sources",
        type=Path,
        default=project_root() / "config" / "live_sources.json",
        help="Path to the live source configuration file.",
    )
    dashboard_parser.add_argument(
        "--rules",
        type=Path,
        default=project_root() / "config" / "impact_rules.json",
        help="Impact rule configuration path.",
    )
    dashboard_parser.add_argument(
        "--universe",
        type=Path,
        default=project_root() / "config" / "instrument_universe.json",
        help="Instrument universe configuration path.",
    )
    dashboard_parser.add_argument(
        "--database",
        type=Path,
        default=project_root() / "data" / "market_news_live.db",
        help="SQLite database path for live run persistence.",
    )
    dashboard_parser.add_argument(
        "--report-dir",
        type=Path,
        default=project_root() / "reports" / "live",
        help="Directory for generated live markdown/json reports.",
    )
    dashboard_parser.add_argument(
        "--user-agent",
        default=os.environ.get("MARKET_NEWS_USER_AGENT", default_user_agent()),
        help="HTTP User-Agent for live source fetching.",
    )

    notify_parser = subparsers.add_parser(
        "notify",
        help="Send high-priority alerts to your phone through OpenClaw.",
    )
    notify_parser.add_argument(
        "--report",
        type=Path,
        default=default_live_report_path(),
        help="Path to a generated report JSON file.",
    )
    notify_parser.add_argument(
        "--preview",
        type=Path,
        default=default_phone_preview_path(),
        help="Path to write the outgoing phone-alert preview.",
    )
    notify_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Fetch fresh live data before composing the mobile alert.",
    )
    notify_parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep refreshing and sending alerts on an interval.",
    )
    notify_parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Refresh interval in seconds when using --watch.",
    )
    notify_parser.add_argument(
        "--sources",
        type=Path,
        default=project_root() / "config" / "live_sources.json",
        help="Path to the live source configuration file.",
    )
    notify_parser.add_argument(
        "--rules",
        type=Path,
        default=project_root() / "config" / "impact_rules.json",
        help="Impact rule configuration path.",
    )
    notify_parser.add_argument(
        "--universe",
        type=Path,
        default=project_root() / "config" / "instrument_universe.json",
        help="Instrument universe configuration path.",
    )
    notify_parser.add_argument(
        "--database",
        type=Path,
        default=project_root() / "data" / "market_news_live.db",
        help="SQLite database path for live run persistence and alert delivery history.",
    )
    notify_parser.add_argument(
        "--report-dir",
        type=Path,
        default=project_root() / "reports" / "live",
        help="Directory for generated live markdown/json reports.",
    )
    notify_parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Maximum number of ranked events and instruments in generated reports.",
    )
    notify_parser.add_argument(
        "--user-agent",
        default=os.environ.get("MARKET_NEWS_USER_AGENT", default_user_agent()),
        help="HTTP User-Agent for live source fetching.",
    )
    notify_parser.add_argument(
        "--channel",
        default="whatsapp",
        help="OpenClaw channel to deliver the alert to.",
    )
    notify_parser.add_argument(
        "--target",
        default=None,
        help="Explicit OpenClaw target. Defaults to the first allowFrom target in ~/.openclaw/openclaw.json.",
    )
    notify_parser.add_argument(
        "--openclaw-bin",
        type=Path,
        default=Path.home() / ".openclaw" / "bin" / "openclaw",
        help="Path to the OpenClaw executable.",
    )
    notify_parser.add_argument(
        "--openclaw-config",
        type=Path,
        default=Path.home() / ".openclaw" / "openclaw.json",
        help="Path to the OpenClaw config file.",
    )
    notify_parser.add_argument(
        "--min-level",
        choices=[level.value for level in AlertLevel],
        default=AlertLevel.HIGH.value,
        help="Minimum alert level that can trigger a mobile message.",
    )
    notify_parser.add_argument(
        "--max-alerts",
        type=int,
        default=3,
        help="Maximum number of alerts to include in one phone message.",
    )
    notify_parser.add_argument(
        "--include-existing",
        action="store_true",
        help="Include high-priority alerts even if they were not marked new in the latest run.",
    )
    notify_parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore recent delivery history and send the alert anyway.",
    )
    notify_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate the phone-message preview without sending it.",
    )
    notify_parser.add_argument(
        "--status-file",
        type=Path,
        default=default_delivery_status_path(),
        help="Path to the latest delivery-line status JSON file.",
    )
    notify_parser.add_argument(
        "--history-file",
        type=Path,
        default=default_delivery_history_path(),
        help="Path to the append-only delivery-line history JSONL file.",
    )

    probe_parser = subparsers.add_parser(
        "probe",
        help="Send a synthetic test message to validate the OpenClaw phone delivery chain.",
    )
    probe_parser.add_argument(
        "--channel",
        default="whatsapp",
        help="OpenClaw channel to deliver the test message to.",
    )
    probe_parser.add_argument(
        "--target",
        default=None,
        help="Explicit OpenClaw target. Defaults to the first allowFrom target in ~/.openclaw/openclaw.json.",
    )
    probe_parser.add_argument(
        "--openclaw-bin",
        type=Path,
        default=Path.home() / ".openclaw" / "bin" / "openclaw",
        help="Path to the OpenClaw executable.",
    )
    probe_parser.add_argument(
        "--openclaw-config",
        type=Path,
        default=Path.home() / ".openclaw" / "openclaw.json",
        help="Path to the OpenClaw config file.",
    )
    probe_parser.add_argument(
        "--preview",
        type=Path,
        default=default_probe_preview_path(),
        help="Path to write the outgoing test-message preview.",
    )
    probe_parser.add_argument(
        "--database",
        type=Path,
        default=project_root() / "data" / "market_news_live.db",
        help="SQLite database path used by the notification runtime.",
    )
    probe_parser.add_argument(
        "--status-file",
        type=Path,
        default=default_monitor_status_path(),
        help="Optional monitor status file to embed recent run context in the probe message.",
    )
    probe_parser.add_argument(
        "--message",
        default=None,
        help="Optional custom test message. If omitted, a built-in probe template is used.",
    )
    probe_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate the test-message preview without sending it.",
    )

    health_parser = subparsers.add_parser(
        "health",
        help="Run an isolated health check against line status files.",
    )
    health_parser.add_argument(
        "--status-file",
        type=Path,
        default=default_health_status_path(),
        help="Path to the latest health summary JSON file.",
    )
    health_parser.add_argument(
        "--history-file",
        type=Path,
        default=default_health_history_path(),
        help="Path to the append-only health history JSONL file.",
    )
    health_parser.add_argument(
        "--max-age",
        type=int,
        default=900,
        help="Maximum allowed heartbeat age in seconds before a line is stale.",
    )
    health_parser.add_argument(
        "--watch",
        action="store_true",
        help="Continuously run health checks.",
    )
    health_parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Refresh interval in seconds when using --watch.",
    )
    health_parser.add_argument(
        "--status",
        dest="status_inputs",
        action="append",
        type=Path,
        default=[],
        help="Explicit status JSON file to check. Can be provided multiple times.",
    )
    health_parser.add_argument(
        "--auto-heal",
        action="store_true",
        help="Automatically restart stale or unhealthy runtime lines before writing the health snapshot.",
    )

    review_api_parser = subparsers.add_parser(
        "review-api",
        help="Run the local lexicon review API used by the web dashboard.",
    )
    review_api_parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind.")
    review_api_parser.add_argument("--port", type=int, default=8765, help="TCP port for the review API.")
    review_api_parser.add_argument(
        "--status-file",
        type=Path,
        default=default_review_api_status_path(),
        help="Path to the review-api status file.",
    )
    review_api_parser.add_argument(
        "--history-file",
        type=Path,
        default=default_review_api_history_path(),
        help="Path to the append-only review-api history JSONL file.",
    )
    review_api_parser.add_argument(
        "--lexicon",
        type=Path,
        default=project_root() / "config" / "tech_lexicon.json",
        help="Current tech lexicon JSON file.",
    )
    review_api_parser.add_argument(
        "--discovery-file",
        type=Path,
        default=default_lexicon_discovery_path(),
        help="JSONL file used to store unknown-term discoveries.",
    )
    review_api_parser.add_argument(
        "--tech-config",
        type=Path,
        default=default_tech_block_config_path(),
        help="Tech block config JSON path.",
    )
    review_api_parser.add_argument(
        "--heartbeat-interval",
        type=int,
        default=60,
        help="Seconds between review-api heartbeats.",
    )
    review_api_parser.add_argument(
        "--auto-review",
        dest="auto_review",
        action="store_true",
        help="Automatically let AI accept or reject newly discovered lexicon terms.",
    )
    review_api_parser.add_argument(
        "--no-auto-review",
        dest="auto_review",
        action="store_false",
        help="Disable background AI lexicon auto-review.",
    )
    review_api_parser.set_defaults(auto_review=_env_flag("MARKET_NEWS_LEXICON_AUTO_REVIEW", True))
    review_api_parser.add_argument(
        "--auto-review-interval",
        type=int,
        default=int(os.environ.get("MARKET_NEWS_LEXICON_AUTO_REVIEW_INTERVAL", "900") or "900"),
        help="Seconds between background AI lexicon auto-review cycles.",
    )
    review_api_parser.add_argument(
        "--auto-review-batch-limit",
        type=int,
        default=int(os.environ.get("MARKET_NEWS_LEXICON_AUTO_REVIEW_BATCH_LIMIT", "40") or "40"),
        help="Maximum lexicon candidates per AI auto-review call.",
    )

    lexicon_parser = subparsers.add_parser(
        "lexicon",
        help="Maintain the A/H tech lexicon feedback and release workflow.",
    )
    lexicon_subparsers = lexicon_parser.add_subparsers(dest="lexicon_command", required=True)

    feedback_parser = lexicon_subparsers.add_parser(
        "feedback",
        help="Record feedback for a tech signal from the latest report.",
    )
    feedback_parser.add_argument("--signal-id", required=True, help="Tech signal cluster_id.")
    feedback_parser.add_argument("--result", required=True, choices=["good", "bad"], help="Feedback result.")
    feedback_parser.add_argument("--note", default="", help="Optional reviewer note.")
    feedback_parser.add_argument(
        "--report",
        type=Path,
        default=default_live_report_path(),
        help="Path to the latest live report JSON.",
    )
    feedback_parser.add_argument(
        "--feedback-file",
        type=Path,
        default=default_lexicon_feedback_path(),
        help="JSONL file used to store lexicon feedback.",
    )

    suggest_parser = lexicon_subparsers.add_parser(
        "suggest",
        help="Generate confidence-adjustment suggestions from feedback history.",
    )
    suggest_parser.add_argument(
        "--feedback-file",
        type=Path,
        default=default_lexicon_feedback_path(),
        help="JSONL file used to store lexicon feedback.",
    )
    suggest_parser.add_argument(
        "--lexicon",
        type=Path,
        default=project_root() / "config" / "tech_lexicon.json",
        help="Current tech lexicon JSON file.",
    )
    suggest_parser.add_argument(
        "--output",
        type=Path,
        default=default_lexicon_suggestions_path(),
        help="Path to write the suggestion report JSON.",
    )
    suggest_parser.add_argument(
        "--min-feedback-count",
        type=int,
        default=5,
        help="Minimum feedback count required before suggesting a confidence change.",
    )

    bump_parser = lexicon_subparsers.add_parser(
        "bump",
        help="Apply approved lexicon confidence changes and bump the release metadata.",
    )
    bump_parser.add_argument(
        "--feedback-file",
        type=Path,
        default=default_lexicon_feedback_path(),
        help="JSONL file used to store lexicon feedback.",
    )
    bump_parser.add_argument(
        "--lexicon",
        type=Path,
        default=project_root() / "config" / "tech_lexicon.json",
        help="Current tech lexicon JSON file.",
    )
    bump_parser.add_argument(
        "--release",
        type=Path,
        default=project_root() / "config" / "tech_lexicon_release.json",
        help="Current lexicon release metadata JSON file.",
    )
    bump_parser.add_argument(
        "--output",
        type=Path,
        default=default_lexicon_suggestions_path(),
        help="Optional path to also write the applied suggestion report JSON.",
    )
    bump_parser.add_argument(
        "--min-feedback-count",
        type=int,
        default=5,
        help="Minimum feedback count required before suggesting a confidence change.",
    )
    bump_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the new lexicon and release files.",
    )
    bump_parser.add_argument(
        "--reviewer",
        default="codex",
        help="Reviewer name recorded in the new lexicon release.",
    )

    discover_parser = lexicon_subparsers.add_parser(
        "discover",
        help="List pending unknown-term candidates discovered from recent runs.",
    )
    discover_parser.add_argument(
        "--discovery-file",
        type=Path,
        default=default_lexicon_discovery_path(),
        help="JSONL file used to store unknown-term discoveries.",
    )
    discover_parser.add_argument(
        "--min-score",
        type=float,
        default=2.0,
        help="Minimum discovery score required to show a candidate.",
    )
    discover_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of candidates to print.",
    )

    add_parser = lexicon_subparsers.add_parser(
        "add",
        help="Accept a discovered term and append a starter entry to the tech lexicon.",
    )
    add_parser.add_argument("term", help="Discovered term text to accept.")
    add_parser.add_argument(
        "--type",
        dest="term_type",
        choices=["theme", "tech", "company", "catalyst", "risk", "policy"],
        default="theme",
        help="term_type used for the generated lexicon starter entry.",
    )
    add_parser.add_argument(
        "--lexicon",
        type=Path,
        default=project_root() / "config" / "tech_lexicon.json",
        help="Current tech lexicon JSON file.",
    )
    add_parser.add_argument(
        "--discovery-file",
        type=Path,
        default=default_lexicon_discovery_path(),
        help="JSONL file used to store unknown-term discoveries.",
    )

    reject_parser = lexicon_subparsers.add_parser(
        "reject",
        help="Reject a discovered term so it no longer appears in review lists.",
    )
    reject_parser.add_argument("term", help="Discovered term text to reject.")
    reject_parser.add_argument(
        "--lexicon",
        type=Path,
        default=project_root() / "config" / "tech_lexicon.json",
        help="Current tech lexicon JSON file.",
    )
    reject_parser.add_argument(
        "--discovery-file",
        type=Path,
        default=default_lexicon_discovery_path(),
        help="JSONL file used to store unknown-term discoveries.",
    )

    remove_parser = lexicon_subparsers.add_parser(
        "remove",
        help="Remove an already accepted term from the formal tech lexicon.",
    )
    remove_parser.add_argument("term", help="Canonical term or synonym to remove from the lexicon.")
    remove_parser.add_argument(
        "--lexicon",
        type=Path,
        default=project_root() / "config" / "tech_lexicon.json",
        help="Current tech lexicon JSON file.",
    )
    remove_parser.add_argument(
        "--discovery-file",
        type=Path,
        default=default_lexicon_discovery_path(),
        help="JSONL file used to store unknown-term discoveries.",
    )

    cookies_parser = subparsers.add_parser(
        "cookies",
        help="Manage local cookie files for the Weibo and Xueqiu collectors.",
    )
    cookies_subparsers = cookies_parser.add_subparsers(dest="cookies_command", required=True)

    cookies_check_parser = cookies_subparsers.add_parser(
        "check",
        help="Check whether local Weibo and Xueqiu cookies still work.",
    )
    cookies_check_parser.add_argument(
        "--sources",
        type=Path,
        default=project_root() / "config" / "live_sources.json",
        help="Path to the live source configuration file.",
    )
    cookies_check_parser.add_argument(
        "--user-agent",
        default=os.environ.get("MARKET_NEWS_USER_AGENT", default_user_agent()),
        help="HTTP User-Agent used for cookie validation.",
    )

    cookies_set_weibo_parser = cookies_subparsers.add_parser(
        "set-weibo",
        help="Install a Weibo cookie export into ~/.market_news.",
    )
    cookies_set_weibo_parser.add_argument("--cookie-file", type=Path, required=True, help="Source cookie JSON file.")

    cookies_set_xueqiu_parser = cookies_subparsers.add_parser(
        "set-xueqiu",
        help="Install a Xueqiu cookie export into ~/.market_news.",
    )
    cookies_set_xueqiu_parser.add_argument("--cookie-file", type=Path, required=True, help="Source cookie JSON file.")

    news_learning_parser = subparsers.add_parser(
        "news-learning",
        help="Generate research-only Evidence-to-Review artifacts for the news collector.",
    )
    news_learning_parser.add_argument(
        "--report",
        type=Path,
        default=default_live_report_path(),
        help="Input report JSON generated by the news collector.",
    )
    news_learning_parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_news_learning_dir(),
        help="Directory for generated evidence, attribution, candidates, and review packet.",
    )
    news_learning_parser.add_argument(
        "--min-source-sample",
        type=int,
        default=3,
        help="Minimum sample size before proposing source-level ranking candidates.",
    )
    news_learning_parser.add_argument(
        "--min-topic-sample",
        type=int,
        default=3,
        help="Minimum sample size before proposing topic-level candidates.",
    )
    news_learning_parser.add_argument(
        "--stale-seconds",
        type=int,
        default=24 * 60 * 60,
        help="Latency threshold after which a claim is labeled stale by default.",
    )

    news_learning_build_parser = subparsers.add_parser(
        "news-learning-build",
        help="Build news Evidence-to-Review artifacts and Codex handoff file.",
    )
    news_learning_build_parser.add_argument(
        "--report",
        type=Path,
        default=default_live_report_path(),
        help="Input report JSON generated by the news collector.",
    )
    news_learning_build_parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_news_learning_dir(),
        help="Directory for generated evidence, attribution, candidates, and review packet.",
    )
    news_learning_build_parser.add_argument(
        "--min-source-sample",
        type=int,
        default=3,
        help="Minimum sample size before proposing source-level ranking candidates.",
    )
    news_learning_build_parser.add_argument(
        "--min-topic-sample",
        type=int,
        default=3,
        help="Minimum sample size before proposing topic-level candidates.",
    )
    news_learning_build_parser.add_argument(
        "--stale-seconds",
        type=int,
        default=24 * 60 * 60,
        help="Latency threshold after which a claim is labeled stale by default.",
    )

    news_learning_export_parser = subparsers.add_parser(
        "news-learning-export",
        help="Build and export the latest news learning handoff for Codex review.",
    )
    news_learning_export_parser.add_argument(
        "--report",
        type=Path,
        default=default_live_report_path(),
        help="Input report JSON generated by the news collector.",
    )
    news_learning_export_parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_news_learning_dir(),
        help="Directory for generated evidence, attribution, candidates, and review packet.",
    )
    news_learning_export_parser.add_argument(
        "--min-source-sample",
        type=int,
        default=3,
        help="Minimum sample size before proposing source-level ranking candidates.",
    )
    news_learning_export_parser.add_argument(
        "--min-topic-sample",
        type=int,
        default=3,
        help="Minimum sample size before proposing topic-level candidates.",
    )
    news_learning_export_parser.add_argument(
        "--stale-seconds",
        type=int,
        default=24 * 60 * 60,
        help="Latency threshold after which a claim is labeled stale by default.",
    )
    news_learning_export_parser.add_argument(
        "--copy",
        dest="copy_to_clipboard",
        action="store_true",
        default=True,
        help="Copy the Codex handoff Markdown to the macOS clipboard.",
    )
    news_learning_export_parser.add_argument(
        "--no-copy",
        dest="copy_to_clipboard",
        action="store_false",
        help="Do not copy the Codex handoff Markdown to the clipboard.",
    )

    news_learning_status_parser = subparsers.add_parser(
        "news-learning-status",
        help="Show the latest news Evidence-to-Review packet status.",
    )
    news_learning_status_parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_news_learning_dir(),
        help="Directory containing generated news Evidence-to-Review artifacts.",
    )

    news_learning_auto_parser = subparsers.add_parser(
        "news-learning-auto",
        help="Run one automated news learning export cycle and write health status.",
    )
    news_learning_auto_parser.add_argument(
        "--report",
        type=Path,
        default=default_live_report_path(),
        help="Input report JSON generated by the news collector.",
    )
    news_learning_auto_parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_news_learning_dir(),
        help="Directory for generated evidence, attribution, candidates, and review packet.",
    )
    news_learning_auto_parser.add_argument(
        "--min-source-sample",
        type=int,
        default=3,
        help="Minimum sample size before proposing source-level ranking candidates.",
    )
    news_learning_auto_parser.add_argument(
        "--min-topic-sample",
        type=int,
        default=3,
        help="Minimum sample size before proposing topic-level candidates.",
    )
    news_learning_auto_parser.add_argument(
        "--stale-seconds",
        type=int,
        default=24 * 60 * 60,
        help="Latency threshold after which a claim is labeled stale by default.",
    )
    news_learning_auto_parser.add_argument(
        "--copy",
        dest="copy_to_clipboard",
        action="store_true",
        default=False,
        help="Copy the Codex handoff Markdown to the macOS clipboard.",
    )
    news_learning_auto_parser.add_argument(
        "--no-copy",
        dest="copy_to_clipboard",
        action="store_false",
        help="Do not copy the Codex handoff Markdown to the clipboard.",
    )
    news_learning_auto_parser.add_argument(
        "--status-file",
        type=Path,
        default=default_news_learning_status_path(),
        help="Path to the latest news-learning automation status JSON file.",
    )
    news_learning_auto_parser.add_argument(
        "--history-file",
        type=Path,
        default=default_news_learning_history_path(),
        help="Path to the append-only news-learning automation history JSONL file.",
    )

    news_learning_codex_review_parser = subparsers.add_parser(
        "news-learning-codex-review",
        help="Ask Codex to review the latest news learning packet and optionally notify you.",
    )
    news_learning_codex_review_parser.add_argument(
        "--report",
        type=Path,
        default=default_live_report_path(),
        help="Input report JSON generated by the news collector.",
    )
    news_learning_codex_review_parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_news_learning_dir(),
        help="Directory for generated evidence, attribution, candidates, and review packet.",
    )
    news_learning_codex_review_parser.add_argument(
        "--analysis-path",
        type=Path,
        default=default_news_learning_codex_analysis_path(),
        help="Where to write Codex's final review message.",
    )
    news_learning_codex_review_parser.add_argument(
        "--min-source-sample",
        type=int,
        default=3,
        help="Minimum sample size before proposing source-level ranking candidates.",
    )
    news_learning_codex_review_parser.add_argument(
        "--min-topic-sample",
        type=int,
        default=3,
        help="Minimum sample size before proposing topic-level candidates.",
    )
    news_learning_codex_review_parser.add_argument(
        "--stale-seconds",
        type=int,
        default=24 * 60 * 60,
        help="Latency threshold after which a claim is labeled stale by default.",
    )
    news_learning_codex_review_parser.add_argument(
        "--status-file",
        type=Path,
        default=default_news_learning_codex_review_status_path(),
        help="Path to the latest Codex-review automation status JSON file.",
    )
    news_learning_codex_review_parser.add_argument(
        "--history-file",
        type=Path,
        default=default_news_learning_codex_review_history_path(),
        help="Path to the append-only Codex-review automation history JSONL file.",
    )
    news_learning_codex_review_parser.add_argument(
        "--codex-bin",
        type=Path,
        default=Path("/Applications/Codex.app/Contents/Resources/codex"),
        help="Path to the Codex CLI binary.",
    )
    news_learning_codex_review_parser.add_argument(
        "--model",
        default="",
        help="Optional Codex model override. Leave empty to use the current Codex default.",
    )
    news_learning_codex_review_parser.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="Maximum seconds to wait for Codex analysis.",
    )
    news_learning_codex_review_parser.add_argument(
        "--notify",
        dest="notify",
        action="store_true",
        default=True,
        help="Send actionable review summaries through OpenClaw.",
    )
    news_learning_codex_review_parser.add_argument(
        "--no-notify",
        dest="notify",
        action="store_false",
        help="Do not send OpenClaw notifications.",
    )
    news_learning_codex_review_parser.add_argument(
        "--notify-all",
        action="store_true",
        help="Notify even when Codex says no change is needed.",
    )
    news_learning_codex_review_parser.add_argument(
        "--channel",
        default="whatsapp",
        help="OpenClaw channel to use for actionable review messages.",
    )
    news_learning_codex_review_parser.add_argument(
        "--target",
        default=None,
        help="Explicit OpenClaw target. Defaults to the first allowFrom target in ~/.openclaw/openclaw.json.",
    )
    news_learning_codex_review_parser.add_argument(
        "--openclaw-bin",
        type=Path,
        default=Path.home() / ".openclaw" / "bin" / "openclaw",
        help="Path to the OpenClaw CLI binary.",
    )
    news_learning_codex_review_parser.add_argument(
        "--openclaw-config",
        type=Path,
        default=Path.home() / ".openclaw" / "openclaw.json",
        help="Path to OpenClaw config.",
    )

    monitor_parser = subparsers.add_parser(
        "monitor",
        help="Run the full live chain: refresh, rank, render dashboard, and push phone alerts.",
    )
    monitor_parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Maximum number of events and instruments to render.",
    )
    monitor_parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Refresh interval in seconds when using --watch.",
    )
    monitor_parser.add_argument(
        "--auto-repair",
        action="store_true",
        default=_env_flag("MARKET_NEWS_AUTO_REPAIR"),
        help="Retry an empty live collection once before marking the cycle degraded.",
    )
    monitor_parser.add_argument(
        "--repair-delay",
        type=int,
        default=20,
        help="Seconds to wait before retrying an empty live collection when auto repair is enabled.",
    )
    monitor_parser.add_argument(
        "--skip-news-learning",
        action="store_true",
        help="Do not generate news Evidence-to-Review artifacts after each monitor cycle.",
    )
    monitor_parser.add_argument(
        "--news-learning-dir",
        type=Path,
        default=default_news_learning_dir(),
        help="Directory for automatic news Evidence-to-Review artifacts.",
    )
    monitor_parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep refreshing the end-to-end chain continuously.",
    )
    monitor_parser.add_argument(
        "--report",
        type=Path,
        default=default_live_report_path(),
        help="Path to the generated report JSON file.",
    )
    monitor_parser.add_argument(
        "--preview",
        type=Path,
        default=default_phone_preview_path(),
        help="Path to write the outgoing phone-alert preview.",
    )
    monitor_parser.add_argument(
        "--status-file",
        type=Path,
        default=default_monitor_status_path(),
        help="Path to the latest monitor health/status JSON file.",
    )
    monitor_parser.add_argument(
        "--history-file",
        type=Path,
        default=default_monitor_history_path(),
        help="Path to the append-only monitor history JSONL file.",
    )
    monitor_parser.add_argument(
        "--sources",
        type=Path,
        default=project_root() / "config" / "live_sources.json",
        help="Path to the live source configuration file.",
    )
    monitor_parser.add_argument(
        "--rules",
        type=Path,
        default=project_root() / "config" / "impact_rules.json",
        help="Impact rule configuration path.",
    )
    monitor_parser.add_argument(
        "--universe",
        type=Path,
        default=project_root() / "config" / "instrument_universe.json",
        help="Instrument universe configuration path.",
    )
    monitor_parser.add_argument(
        "--database",
        type=Path,
        default=project_root() / "data" / "market_news_live.db",
        help="SQLite database path for live runs and delivery history.",
    )
    monitor_parser.add_argument(
        "--report-dir",
        type=Path,
        default=project_root() / "reports" / "live",
        help="Directory for generated live markdown/json reports.",
    )
    monitor_parser.add_argument(
        "--user-agent",
        default=os.environ.get("MARKET_NEWS_USER_AGENT", default_user_agent()),
        help="HTTP User-Agent for live source fetching.",
    )
    monitor_parser.add_argument(
        "--channel",
        default="whatsapp",
        help="OpenClaw channel to deliver alerts to.",
    )
    monitor_parser.add_argument(
        "--target",
        default=None,
        help="Explicit OpenClaw target. Defaults to the first allowFrom target in ~/.openclaw/openclaw.json.",
    )
    monitor_parser.add_argument(
        "--openclaw-bin",
        type=Path,
        default=Path.home() / ".openclaw" / "bin" / "openclaw",
        help="Path to the OpenClaw executable.",
    )
    monitor_parser.add_argument(
        "--openclaw-config",
        type=Path,
        default=Path.home() / ".openclaw" / "openclaw.json",
        help="Path to the OpenClaw config file.",
    )
    monitor_parser.add_argument(
        "--min-level",
        choices=[level.value for level in AlertLevel],
        default=AlertLevel.HIGH.value,
        help="Minimum alert level that can trigger a phone message.",
    )
    monitor_parser.add_argument(
        "--max-alerts",
        type=int,
        default=3,
        help="Maximum number of alerts to include in one phone message.",
    )
    monitor_parser.add_argument(
        "--include-existing",
        action="store_true",
        help="Include high-priority alerts even if they were not marked new in the latest run.",
    )
    monitor_parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore recent delivery history and send the alert anyway.",
    )
    monitor_parser.add_argument(
        "--skip-notify",
        action="store_true",
        help="Run the live pipeline and dashboard only, without phone delivery.",
    )
    monitor_parser.add_argument(
        "--dry-run-notify",
        action="store_true",
        help="Generate the phone-alert preview without sending it.",
    )

    # AH multi-factor scanner — additive subcommand. Reads from Futu OpenD,
    # writes report sidecars under reports/live/. Independent of the live /
    # collect / notify lines.
    ah_scan_parser = subparsers.add_parser(
        "ah-scan",
        help="Run the AH multi-factor scanner (limit-up streak / volume shrink / near ATH) via Futu OpenD.",
    )
    ah_scan_parser.add_argument(
        "--markets",
        default=os.environ.get("MARKET_NEWS_AH_SCANNER_MARKETS", "HK,SH,SZ"),
        help="Comma-separated market codes to include (HK,SH,SZ,US). Default HK,SH,SZ.",
    )
    ah_scan_parser.add_argument(
        "--top",
        type=int,
        default=int(os.environ.get("MARKET_NEWS_AH_SCANNER_TOP_N", "30")),
        help="Max rows per board. Default 30.",
    )
    ah_scan_parser.add_argument(
        "--update-universe",
        action="store_true",
        help="Also write a config/tech_universe_cn_hk.dynamic.json file. Off by default; "
             "even when written, the pipeline only loads it if MARKET_NEWS_TECH_UNIVERSE_DYNAMIC=1.",
    )
    return parser


def build_pipeline(
    *,
    collector: object,
    rules: Path,
    universe: Path,
    database: Path,
    report_dir: Path,
    top: int,
) -> MarketNewsPipeline:
    base_impact_analyzer = ConfigDrivenImpactAnalyzer.from_file(rules)
    base_instrument_mapper = ConfigDrivenInstrumentMapper.from_file(universe)
    impact_analyzer, instrument_mapper, _ = build_model_judgement_stack(
        config_path=default_model_judgement_config_path(),
        project_root=project_root(),
        base_impact_analyzer=base_impact_analyzer,
        base_instrument_mapper=base_instrument_mapper,
        candidate_instruments=_load_model_candidate_instruments(base_instrument_mapper),
    )
    # Optional: AH scanner-built dynamic universe (default off).
    # Behavior is bit-for-bit identical to the static universe when the flag
    # is off OR when the dynamic file does not exist. Existing pipelines see
    # no change unless the user explicitly opts in.
    _static_universe_path = project_root() / "config" / "tech_universe_cn_hk.json"
    _dynamic_universe_path = project_root() / "config" / "tech_universe_cn_hk.dynamic.json"
    _use_dynamic = _env_flag("MARKET_NEWS_TECH_UNIVERSE_DYNAMIC") and _dynamic_universe_path.exists()
    _universe_path = _dynamic_universe_path if _use_dynamic else _static_universe_path
    tech_block = AHShareTechFeatureBlock.from_files(
        universe_path=_universe_path,
        lexicon_path=project_root() / "config" / "tech_lexicon.json",
        lexicon_release_path=project_root() / "config" / "tech_lexicon_release.json",
        graph_path=project_root() / "config" / "tech_impact_graph.json",
        frontier_map_path=project_root() / "config" / "tech_frontier_map.json",
        config_path=default_tech_block_config_path(),
        top_n=min(top, 8),
    )
    return MarketNewsPipeline(
        collector=collector,
        normalizer=DefaultNormalizer(),
        deduplicator=FingerprintDeduplicator(),
        clusterer=KeywordEventClusterer(),
        impact_analyzer=impact_analyzer,
        event_ranker=WeightedEventRanker(),
        instrument_mapper=instrument_mapper,
        instrument_ranker=WeightedInstrumentRanker(),
        alert_engine=RuleBasedAlertEngine(),
        store=SQLiteRunStore(database),
        reporter=MarkdownJsonReporter(
            report_dir,
            top_n=top,
            lexicon_discovery_path=default_lexicon_discovery_path(),
            lexicon_path=project_root() / "config" / "tech_lexicon.json",
            tech_block_config_path=default_tech_block_config_path(),
        ),
        feature_modules=[tech_block],
    )


def _load_model_candidate_instruments(
    base_mapper: ConfigDrivenInstrumentMapper,
) -> list[object]:
    instruments = list(base_mapper.instruments)
    seen = {instrument.symbol.upper() for instrument in instruments}
    tech_path = project_root() / "config" / "tech_universe_cn_hk.json"
    if not tech_path.exists():
        return instruments
    try:
        payload = json.loads(tech_path.read_text(encoding="utf-8"))
    except Exception:
        return instruments
    if not isinstance(payload, list):
        return instruments
    from market_news.domain.models import InstrumentDescriptor, Market

    for item in payload:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip()
        market = str(item.get("market") or "").strip()
        if not symbol or not market or symbol.upper() in seen:
            continue
        try:
            descriptor = InstrumentDescriptor(
                symbol=symbol,
                market=Market(market),
                asset_type=str(item.get("asset_type") or "stock"),
                name=str(item.get("name") or symbol),
                sectors=[
                    str(value).strip()
                    for value in item.get("sectors", [])
                    if str(value).strip()
                ],
                themes=[
                    str(value).strip()
                    for value in item.get("themes", [])
                    if str(value).strip()
                ],
                aliases=[
                    str(value).strip()
                    for value in item.get("aliases", [])
                    if str(value).strip()
                ],
                liquidity_score=float(item.get("liquidity_score", 0.6)),
                metadata={"generated_from": "tech_universe_cn_hk"},
            )
        except (TypeError, ValueError):
            continue
        instruments.append(descriptor)
        seen.add(symbol.upper())
    return instruments


def _report_has_content(report_path: Path) -> bool:
    if not report_path.exists():
        return False
    try:
        payload = _load_json(report_path)
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    counts = payload.get("counts", {})
    if isinstance(counts, dict):
        for key in ("raw_records", "documents", "clusters", "ranked_events"):
            if int(counts.get(key, 0) or 0) > 0:
                return True
    return bool(payload.get("top_events") or payload.get("latest_feed"))


def _backup_report_bundle(report_dir: Path) -> dict[str, bytes]:
    report_path = report_dir / "latest_report.json"
    if not _report_has_content(report_path):
        return {}
    bundle: dict[str, bytes] = {}
    for name in ("latest_report.json", "latest_report.md", "latest_dashboard.html"):
        path = report_dir / name
        if path.exists():
            bundle[name] = path.read_bytes()
    return bundle


def _restore_report_bundle(report_dir: Path, bundle: dict[str, bytes]) -> None:
    if not bundle:
        return
    report_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in bundle.items():
        (report_dir / name).write_bytes(payload)


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _bump_release_version(version: str) -> str:
    if "-p" in version:
        head, tail = version.rsplit("-p", 1)
        if tail.isdigit():
            return f"{head}-p{int(tail) + 1}"
    return f"{version}-p1"


def _default_report_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        sources=getattr(args, "sources", project_root() / "config" / "live_sources.json"),
        rules=getattr(args, "rules", project_root() / "config" / "impact_rules.json"),
        universe=getattr(args, "universe", project_root() / "config" / "instrument_universe.json"),
        database=getattr(args, "database", project_root() / "data" / "market_news_live.db"),
        report_dir=getattr(args, "report_dir", project_root() / "reports" / "live"),
        top=getattr(args, "top", 10),
        user_agent=getattr(args, "user_agent", os.environ.get("MARKET_NEWS_USER_AGENT", default_user_agent())),
    )


def _write_cooccurrence(snapshot: object) -> None:
    CooccurrenceMiner(default_cooccurrence_path()).write_from_snapshot(snapshot)


def _load_unknown_term_config(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"enabled": True, "min_freq": 3, "min_discovery_score": 2.0, "max_candidates_per_run": 50}
    payload = _load_json(path)
    if not isinstance(payload, dict):
        return {}
    detector = payload.get("unknown_term_detector", payload)
    return detector if isinstance(detector, dict) else {}


def _write_unknown_term_discovery(snapshot: object) -> dict[str, object]:
    config = _load_unknown_term_config(default_tech_block_config_path())
    if not bool(config.get("enabled", True)):
        summary = {"pending_count": 0, "saved_count": 0, "candidates": [], "enabled": False}
        snapshot.feature_blocks["lexicon_discovery"] = summary
        return summary
    lexicon_payload = _load_json(project_root() / "config" / "tech_lexicon.json")
    if not isinstance(lexicon_payload, list):
        summary = {"pending_count": 0, "saved_count": 0, "candidates": [], "enabled": False}
        snapshot.feature_blocks["lexicon_discovery"] = summary
        return summary
    detector = UnknownTermDetector(lexicon=lexicon_payload, config=config)
    relevant_records = detector.select_relevant_records(snapshot.raw_records)
    candidates = detector.run(relevant_records)
    saved = detector.save(candidates, default_lexicon_discovery_path())
    detector.prune_noise(default_lexicon_discovery_path())
    pending_rows = detector.list_pending(
        default_lexicon_discovery_path(),
        min_score=float(config.get("min_discovery_score", 2.0)),
        limit=max(int(config.get("max_candidates_per_run", 50)), 10),
    )
    summary = {
        "pending_count": len(pending_rows),
        "saved_count": len(saved),
        "relevant_record_count": len(relevant_records),
        "candidates": pending_rows,
        "enabled": True,
    }
    snapshot.feature_blocks["lexicon_discovery"] = summary
    return summary


def _print_snapshot(snapshot: object) -> None:
    print(f"Run ID: {snapshot.run_id}")
    print(f"Documents: {len(snapshot.documents)}")
    print(f"Clusters: {len(snapshot.clusters)}")
    print(f"Ranked events: {len(snapshot.ranked_events)}")
    print(f"Ranked instruments: {len(snapshot.ranked_instruments)}")
    for name, location in snapshot.artifacts.items():
        print(f"{name}: {location}")


def run_pipeline(args: argparse.Namespace) -> int:
    pipeline = build_pipeline(
        collector=LocalJSONCollector(args.input),
        rules=args.rules,
        universe=args.universe,
        database=args.database,
        report_dir=args.report_dir,
        top=args.top,
    )
    snapshot = pipeline.run()
    _write_cooccurrence(snapshot)
    _write_unknown_term_discovery(snapshot)
    snapshot.artifacts.update(
        {
            key: str(path)
            for key, path in pipeline.reporter.write(snapshot).items()
        }
    )
    _print_snapshot(snapshot)
    return 0


def _attach_news_learning_artifacts(
    snapshot: object,
    *,
    report_path: Path,
    output_dir: Path,
) -> None:
    """Generate research-only news learning artifacts without affecting collection decisions."""
    artifacts = getattr(snapshot, "artifacts", None)
    if not isinstance(artifacts, dict):
        return
    if not _report_has_content(report_path):
        artifacts["news_learning_status"] = "skipped-empty-report"
        artifacts["news_learning_error"] = "report has no ranked events or latest feed"
        return
    try:
        result = build_news_learning_artifacts(report_path=report_path, output_dir=output_dir)
    except Exception as exc:
        artifacts["news_learning_status"] = "error"
        artifacts["news_learning_error"] = str(exc)
        return
    artifacts.update(
        {
            "news_learning_status": "ok",
            "news_learning_output_dir": str(result.output_dir),
            "news_learning_review_packet_md": str(result.artifact_paths["news_learning_review_packet_md"]),
            "news_learning_review_packet_json": str(result.artifact_paths["news_learning_review_packet_json"]),
            "news_learning_codex_handoff": str(result.artifact_paths["news_learning_codex_handoff"]),
            "news_learning_candidate_count": len(result.candidates),
        }
    )


def execute_live_pipeline(args: argparse.Namespace) -> object:
    previous_report_bundle = _backup_report_bundle(args.report_dir)
    pipeline = build_pipeline(
        collector=build_live_collector(args.sources, args.user_agent),
        rules=args.rules,
        universe=args.universe,
        database=args.database,
        report_dir=args.report_dir,
        top=args.top,
    )
    snapshot = pipeline.run()
    _write_cooccurrence(snapshot)
    _write_unknown_term_discovery(snapshot)
    snapshot.artifacts.update(
        {
            key: str(path)
            for key, path in pipeline.reporter.write(snapshot).items()
        }
    )
    if len(snapshot.raw_records) == 0 and len(snapshot.documents) == 0:
        snapshot.artifacts["report_fallback_applied"] = bool(previous_report_bundle)
        if previous_report_bundle:
            _restore_report_bundle(args.report_dir, previous_report_bundle)
        snapshot.artifacts["cycle_warning"] = (
            "Live collection returned 0 records; the last non-empty report was preserved."
        )
    if not bool(getattr(args, "skip_news_learning", False)):
        _attach_news_learning_artifacts(
            snapshot,
            report_path=Path(snapshot.artifacts["json_report"]),
            output_dir=Path(getattr(args, "news_learning_dir", default_news_learning_dir())),
        )
    return snapshot


def _execute_live_pipeline_with_auto_repair(args: argparse.Namespace) -> object:
    snapshot = execute_live_pipeline(args)
    warning = str(snapshot.artifacts.get("cycle_warning") or "").strip()
    if not warning or not bool(getattr(args, "auto_repair", False)):
        return snapshot
    repair_delay = max(int(getattr(args, "repair_delay", 20) or 0), 0)
    if repair_delay > 0:
        print(f"Auto-repair: empty live collection detected, retrying in {repair_delay}s...")
        time.sleep(repair_delay)
    repaired_snapshot = execute_live_pipeline(args)
    repaired_warning = str(repaired_snapshot.artifacts.get("cycle_warning") or "").strip()
    if not repaired_warning:
        repaired_snapshot.artifacts["repair_attempts"] = 1
        repaired_snapshot.artifacts["repair_status"] = "recovered"
        repaired_snapshot.artifacts["repair_note"] = warning
        return repaired_snapshot
    repaired_snapshot.artifacts["repair_attempts"] = 2
    repaired_snapshot.artifacts["repair_status"] = "still-empty"
    repaired_snapshot.artifacts["repair_note"] = warning
    return repaired_snapshot


def _collection_overall_status_override(snapshot: object) -> str | None:
    cycle_warning = str(snapshot.artifacts.get("cycle_warning", "") or "").strip()
    if not cycle_warning:
        return None
    if bool(snapshot.artifacts.get("report_fallback_applied")):
        return None
    return "degraded"


def _refresh_health_snapshot_from_runtime(*, max_age_seconds: int = 900) -> None:
    status_writer = HealthStateWriter(
        status_path=default_health_status_path(),
        history_path=default_health_history_path(),
    )
    snapshot = evaluate_status_files(
        status_files=discover_status_files(
            [
                ("collect", default_collect_status_path()),
                ("delivery", default_delivery_status_path()),
                ("news_learning", default_news_learning_status_path()),
                ("review_api", default_review_api_status_path()),
                ("monitor", default_monitor_status_path()),
            ]
        ),
        max_age_seconds=max_age_seconds,
    )
    status_writer.write(snapshot)


def _stack_agents_file() -> Path:
    return project_root() / "runtime" / "stack_agents.txt"


def _load_stack_agents() -> dict[str, str]:
    path = _stack_agents_file()
    if not path.exists():
        return {}
    labels: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        labels[key.strip()] = value.strip()
    return labels


def _launchctl_kickstart(label: str) -> tuple[bool, str]:
    if not label:
        return False, "missing launchd label"
    domain = f"gui/{os.getuid()}/{label}"
    try:
        subprocess.run(
            ["launchctl", "kickstart", "-k", domain],
            check=True,
            capture_output=True,
            text=True,
        )
        return True, domain
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        return False, detail


def _touch_status_timestamp(status_path: Path) -> bool:
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    payload["timestamp"] = utcnow().isoformat()
    try:
        status_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        return False
    return True


def _auto_heal_health_checks(checks: list[object]) -> list[str]:
    labels = _load_stack_agents()
    heal_map = {
        "collect": labels.get("collect_label", ""),
        "delivery": labels.get("notify_label", ""),
        "news_learning": labels.get("news_learning_label", ""),
        "review_api": labels.get("review_api_label", ""),
    }
    repairs: list[str] = []
    for check in checks:
        name = str(getattr(check, "name", "") or "").strip()
        status = str(getattr(check, "status", "") or "").strip().lower()
        if name not in heal_map:
            continue
        if status not in {"stale", "error", "degraded", "missing"}:
            continue
        ok, detail = _launchctl_kickstart(heal_map[name])
        action = "restarted" if ok else "restart-failed"
        if ok:
            _touch_status_timestamp(Path(getattr(check, "status_path", "")))
        repairs.append(f"{name}:{action}:{detail}")
    return repairs


def run_live_pipeline(args: argparse.Namespace) -> int:
    snapshot = execute_live_pipeline(args)
    _print_snapshot(snapshot)
    return 0


def run_dashboard(args: argparse.Namespace) -> int:
    def refresh() -> None:
        if not args.refresh:
            return
        live_args = argparse.Namespace(
            sources=args.sources,
            rules=args.rules,
            universe=args.universe,
            database=args.database,
            report_dir=args.report_dir,
            top=args.top,
            user_agent=args.user_agent,
        )
        run_live_pipeline(live_args)

    if args.watch:
        watch_dashboard(
            args.report,
            top_n=args.top,
            interval_seconds=args.interval,
            refresh_callback=refresh if args.refresh else None,
        )
        return 0

    if args.refresh:
        refresh()
    print(render_dashboard(args.report, top_n=args.top))
    return 0


def _resolve_preview_path(preview_path: Path, report_path: Path) -> Path:
    if preview_path == default_phone_preview_path():
        return report_path.parent / "latest_phone_alert.txt"
    return preview_path


def _build_notification_runner(args: argparse.Namespace) -> NotificationRunner:
    return NotificationRunner(
        store=SQLiteRunStore(args.database),
        notifier=OpenClawNotifier(
            binary_path=args.openclaw_bin,
            config_path=args.openclaw_config,
        ),
    )


def _deliver_notification(
    args: argparse.Namespace,
    runner: NotificationRunner,
    *,
    report_path: Path,
    preview_path: Path,
    dry_run: bool,
    continue_on_error: bool = False,
) -> tuple[NotificationResult, int]:
    try:
        result = runner.deliver_from_report(
            report_path=report_path,
            preview_path=preview_path,
            channel=args.channel,
            target=args.target,
            min_level=AlertLevel(args.min_level),
            max_alerts=args.max_alerts,
            include_existing=args.include_existing,
            force=args.force,
            dry_run=dry_run,
        )
        return result, 0
    except Exception as exc:
        result = NotificationResult(
            status="error",
            channel=getattr(args, "channel", "n/a"),
            target=args.target or "auto",
            alert_count=0,
            preview_path=preview_path,
            cluster_ids=[],
            detail=str(exc),
        )
        if continue_on_error:
            return result, 1
        raise


def _format_notification_lines(result: NotificationResult | None) -> list[str]:
    if result is None:
        return ["Phone alerts: disabled for this run."]
    lines = [
        f"Phone alerts: {result.status}",
        f"Channel: {result.channel}",
        f"Target: {result.target}",
        f"Alerts: {result.alert_count}",
        f"Preview: {result.preview_path}",
        f"Detail: {result.detail}",
    ]
    return lines


def _render_monitor_screen(
    *,
    report_path: Path,
    top_n: int,
    notification_result: NotificationResult | None,
    status_path: Path | None = None,
) -> str:
    dashboard = render_dashboard(report_path, top_n=top_n).rstrip()
    lines = [dashboard, "", "Delivery", "--------"]
    lines.extend(_format_notification_lines(notification_result))
    if status_path is not None:
        lines.extend(["", "Monitor State", "-------------", f"Status file: {status_path}"])
    return "\n".join(lines) + "\n"


def _render_monitor_failure_screen(
    *,
    error_message: str,
    status_path: Path | None = None,
) -> str:
    lines = [
        "Market News Monitor",
        "===================",
        "",
        "Current cycle failed.",
        f"Error: {error_message}",
    ]
    if status_path is not None:
        lines.append(f"Status file: {status_path}")
    return "\n".join(lines) + "\n"


def _clear_console() -> None:
    if not sys.stdout.isatty():
        return
    if not os.environ.get("TERM"):
        return
    os.system("clear")


def _render_collection_screen(
    *,
    report_path: Path,
    top_n: int,
    status_path: Path | None = None,
) -> str:
    dashboard = render_dashboard(report_path, top_n=top_n).rstrip()
    lines = [dashboard, "", "Collection State", "----------------"]
    if status_path is not None:
        lines.append(f"Status file: {status_path}")
    return "\n".join(lines) + "\n"


def run_notify(args: argparse.Namespace) -> int:
    runner = _build_notification_runner(args)
    state_writer = DeliveryStateWriter(
        status_path=args.status_file,
        history_path=args.history_file,
    )

    def cycle() -> int:
        report_path = args.report
        preview_path = _resolve_preview_path(args.preview, report_path)
        try:
            if args.refresh:
                snapshot = _execute_live_pipeline_with_auto_repair(args)
                report_path = Path(snapshot.artifacts["json_report"])
            preview_path = _resolve_preview_path(args.preview, report_path)
            result, exit_code = _deliver_notification(
                args,
                runner,
                report_path=report_path,
                preview_path=preview_path,
                dry_run=args.dry_run,
                continue_on_error=True,
            )
            state_writer.write_cycle(
                report_path=report_path,
                preview_path=preview_path,
                notification_result=result,
            )
            _refresh_health_snapshot_from_runtime()
            refresh_runtime_status_views(report_path)
            print(f"Status: {result.status}")
            print(f"Channel: {result.channel}")
            print(f"Target: {result.target}")
            print(f"Alerts: {result.alert_count}")
            print(f"Preview: {result.preview_path}")
            print(result.detail)
            return exit_code
        except Exception as exc:
            state_writer.write_failure(
                error_message=str(exc),
                report_path=report_path,
                preview_path=preview_path,
            )
            _refresh_health_snapshot_from_runtime()
            refresh_runtime_status_views(report_path)
            raise

    if not args.watch:
        return cycle()

    while True:
        cycle()
        print(f"Next notify run in {args.interval}s. Press Ctrl+C to stop.")
        time.sleep(args.interval)


def run_probe(args: argparse.Namespace) -> int:
    runner = _build_notification_runner(args)
    result = runner.send_probe(
        channel=args.channel,
        target=args.target,
        preview_path=args.preview,
        message=args.message,
        dry_run=args.dry_run,
        status_path=args.status_file,
    )
    print(f"Status: {result.status}")
    print(f"Channel: {result.channel}")
    print(f"Target: {result.target}")
    print(f"Preview: {result.preview_path}")
    print(result.detail)
    return 0


def run_collect(args: argparse.Namespace) -> int:
    state_writer = MonitorStateWriter(
        status_path=args.status_file,
        history_path=args.history_file,
    )

    def cycle() -> int:
        preview_path = _resolve_preview_path(args.preview, default_live_report_path())

        def touch_running() -> None:
            state_writer.write_running(
                report_path=default_live_report_path(),
                preview_path=preview_path,
            )
            _refresh_health_snapshot_from_runtime()
            refresh_runtime_status_views(default_live_report_path())

        stop_running_heartbeat = threading.Event()

        def running_heartbeat_loop() -> None:
            while not stop_running_heartbeat.wait(60):
                touch_running()

        touch_running()
        running_heartbeat = threading.Thread(
            target=running_heartbeat_loop,
            daemon=True,
            name="collect-running-heartbeat",
        )
        running_heartbeat.start()
        try:
            snapshot = _execute_live_pipeline_with_auto_repair(args)
            stop_running_heartbeat.set()
            running_heartbeat.join(timeout=1.0)
            report_path = Path(snapshot.artifacts["json_report"])
            preview_path = _resolve_preview_path(args.preview, report_path)
            cycle_warning = str(snapshot.artifacts.get("cycle_warning", "") or "").strip()
            state_writer.write_cycle(
                snapshot=snapshot,
                report_path=report_path,
                preview_path=preview_path,
                notification_result=None,
                overall_status_override=_collection_overall_status_override(snapshot),
            )
            _refresh_health_snapshot_from_runtime()
            refresh_runtime_status_views(report_path)
            _clear_console()
            print(
                _render_collection_screen(
                    report_path=report_path,
                    top_n=args.top,
                    status_path=state_writer.status_path,
                )
            )
            if snapshot.artifacts.get("repair_status") == "recovered":
                print("Auto-repair: empty cycle recovered on the second attempt.")
            elif cycle_warning:
                print(f"Warning: {cycle_warning}")
            return 0
        except Exception as exc:
            stop_running_heartbeat.set()
            running_heartbeat.join(timeout=1.0)
            state_writer.write_failure(
                error_message=str(exc),
                preview_path=preview_path,
            )
            _refresh_health_snapshot_from_runtime()
            refresh_runtime_status_views(default_live_report_path())
            _clear_console()
            print(
                _render_monitor_failure_screen(
                    error_message=str(exc),
                    status_path=state_writer.status_path,
                )
            )
            return 1

    if not args.watch:
        return cycle()

    while True:
        cycle()
        print(f"Next collect cycle in {args.interval}s. Press Ctrl+C to stop.")
        time.sleep(args.interval)


def run_health(args: argparse.Namespace) -> int:
    state_writer = HealthStateWriter(
        status_path=args.status_file,
        history_path=args.history_file,
    )

    def resolve_inputs() -> list[tuple[str, Path]]:
        if args.status_inputs:
            return [
                (status_path.stem.replace("_status", "") or f"line-{index}", status_path)
                for index, status_path in enumerate(args.status_inputs, start=1)
            ]
        return discover_status_files(
            [
                ("collect", default_collect_status_path()),
                ("delivery", default_delivery_status_path()),
                ("news_learning", default_news_learning_status_path()),
                ("review_api", default_review_api_status_path()),
                ("monitor", default_monitor_status_path()),
            ]
        )

    def cycle() -> int:
        snapshot = evaluate_status_files(
            status_files=resolve_inputs(),
            max_age_seconds=args.max_age,
        )
        repair_actions: list[str] = []
        if bool(getattr(args, "auto_heal", False)) and snapshot.overall_status in {"stale", "degraded", "error"}:
            repair_actions = _auto_heal_health_checks(snapshot.checks)
            if repair_actions:
                time.sleep(2)
                snapshot = evaluate_status_files(
                    status_files=resolve_inputs(),
                    max_age_seconds=args.max_age,
                )
        state_writer.write(snapshot)
        refresh_runtime_status_views(default_live_report_path())
        _clear_console()
        print(render_health_screen(snapshot, status_path=state_writer.status_path))
        if repair_actions:
            print("Auto-heal: " + "; ".join(repair_actions))
        return exit_code_for(snapshot)

    if not args.watch:
        return cycle()

    while True:
        cycle()
        print(f"Next health check in {args.interval}s. Press Ctrl+C to stop.")
        time.sleep(args.interval)


def run_review_api(args: argparse.Namespace) -> int:
    tech_config = _load_json(args.tech_config)
    if not isinstance(tech_config, dict):
        raise SystemExit(f"Tech config must be a JSON object: {args.tech_config}")

    service = LexiconReviewService(
        lexicon_path=args.lexicon,
        discovery_path=args.discovery_file,
        report_path=default_live_report_path(),
        tech_block_config=tech_config,
        status_writer=ReviewApiStateWriter(
            status_path=args.status_file,
            history_path=args.history_file,
        ),
        host=args.host,
        port=args.port,
        ai_client=OpenClawReviewAssistant(
            config_path=default_model_judgement_config_path(),
            project_root=project_root(),
        ),
        auto_review_enabled=bool(args.auto_review),
        auto_review_interval_seconds=args.auto_review_interval,
        auto_review_batch_limit=args.auto_review_batch_limit,
    )
    print(f"Lexicon review API listening on http://{args.host}:{args.port}")
    try:
        serve_review_api(
            service,
            host=args.host,
            port=args.port,
            heartbeat_interval=args.heartbeat_interval,
        )
    except KeyboardInterrupt:
        pass
    return 0


def run_monitor(args: argparse.Namespace) -> int:
    runner = None if args.skip_notify else _build_notification_runner(args)
    state_writer = MonitorStateWriter(
        status_path=args.status_file,
        history_path=args.history_file,
    )

    def cycle() -> int:
        report_path = args.report
        preview_path = args.preview
        notification_result = None
        try:
            snapshot = _execute_live_pipeline_with_auto_repair(args)
            report_path = Path(snapshot.artifacts["json_report"])
            preview_path = _resolve_preview_path(args.preview, report_path)
            cycle_warning = str(snapshot.artifacts.get("cycle_warning", "") or "").strip()
            exit_code = 0
            if runner is not None:
                notification_result, exit_code = _deliver_notification(
                    args,
                    runner,
                    report_path=report_path,
                    preview_path=preview_path,
                    dry_run=args.dry_run_notify,
                    continue_on_error=True,
                )
            state_writer.write_cycle(
                snapshot=snapshot,
                report_path=report_path,
                preview_path=preview_path,
                notification_result=notification_result,
                overall_status_override=_collection_overall_status_override(snapshot),
            )
            _refresh_health_snapshot_from_runtime()
            _clear_console()
            print(
                _render_monitor_screen(
                    report_path=report_path,
                    top_n=args.top,
                    notification_result=notification_result,
                    status_path=state_writer.status_path,
                )
            )
            if snapshot.artifacts.get("repair_status") == "recovered":
                print("Auto-repair: empty cycle recovered on the second attempt.")
            elif cycle_warning:
                print(f"Warning: {cycle_warning}")
            return exit_code
        except Exception as exc:
            state_writer.write_failure(
                error_message=str(exc),
                report_path=report_path,
                preview_path=preview_path,
                notification_result=notification_result,
            )
            _refresh_health_snapshot_from_runtime()
            _clear_console()
            print(
                _render_monitor_failure_screen(
                    error_message=str(exc),
                    status_path=state_writer.status_path,
                )
            )
            return 1

    if not args.watch:
        return cycle()

    while True:
        cycle()
        print(f"Next monitor cycle in {args.interval}s. Press Ctrl+C to stop.")
        time.sleep(args.interval)


def run_lexicon(args: argparse.Namespace) -> int:
    detector_config = _load_unknown_term_config(default_tech_block_config_path())

    if args.lexicon_command == "feedback":
        report_payload = _load_json(args.report)
        tech_block = report_payload.get("tech_block", {}) if isinstance(report_payload, dict) else {}
        signals = tech_block.get("signals", []) if isinstance(tech_block, dict) else []
        signal = next(
            (
                item
                for item in signals
                if isinstance(item, dict) and str(item.get("cluster_id", "")).strip() == args.signal_id
            ),
            None,
        )
        if signal is None:
            raise SystemExit(f"Signal not found in report: {args.signal_id}")
        matched_terms = [
            str(item.get("term", "")).strip()
            for item in signal.get("matched_terms", [])
            if isinstance(item, dict) and str(item.get("term", "")).strip()
        ]
        LexiconFeedbackStore(args.feedback_file).record(
            signal_id=args.signal_id,
            result=args.result,
            matched_terms=matched_terms,
            note=args.note,
        )
        print(f"Recorded feedback for {args.signal_id}: {args.result}")
        print(f"Matched terms: {', '.join(matched_terms) or 'n/a'}")
        print(f"Feedback file: {args.feedback_file}")
        return 0

    if args.lexicon_command == "discover":
        lexicon_payload = _load_json(project_root() / "config" / "tech_lexicon.json")
        if not isinstance(lexicon_payload, list):
            raise SystemExit("Tech lexicon file must be a JSON array.")
        detector = UnknownTermDetector(lexicon=lexicon_payload, config=detector_config)
        pruned = detector.prune_noise(args.discovery_file)
        candidates = detector.list_pending(
            args.discovery_file,
            min_score=args.min_score,
            limit=args.limit,
        )
        print(f"Pending candidates: {len(candidates)}")
        if pruned:
            print(f"Auto-pruned noisy rows: {pruned}")
        for item in candidates:
            impact = item.get("inferred_impact", {})
            if isinstance(impact, dict):
                impact_text = ", ".join(f"{key}:{value}" for key, value in impact.items())
            else:
                impact_text = ""
            print(
                f"- {item.get('text')} | freq={item.get('raw_freq')} | "
                f"score={item.get('discovery_score')} | impact={impact_text or 'n/a'}"
            )
        print(f"Discovery file: {args.discovery_file}")
        return 0

    if args.lexicon_command in {"add", "reject", "remove"}:
        lexicon_payload = _load_json(args.lexicon)
        if not isinstance(lexicon_payload, list):
            raise SystemExit("Tech lexicon file must be a JSON array.")
        detector = UnknownTermDetector(lexicon=lexicon_payload, config=detector_config)

        if args.lexicon_command == "reject":
            candidates = detector.load(args.discovery_file)
            candidate = next(
                (
                    item
                    for item in candidates
                    if str(item.get("text", "")).strip().lower() == args.term.strip().lower()
                ),
                None,
            )
            if candidate is None:
                raise SystemExit(f"Candidate not found in discovery file: {args.term}")
            if detector.set_status(args.discovery_file, args.term, "rejected"):
                print(f"Rejected: {args.term}")
                print(f"Discovery file: {args.discovery_file}")
                return 0
            raise SystemExit(f"Failed to reject candidate: {args.term}")

        if args.lexicon_command == "remove":
            lowered = args.term.strip().lower()
            filtered = [
                item
                for item in lexicon_payload
                if not (
                    str(item.get("canonical_text", "")).strip().lower() == lowered
                    or any(str(synonym).strip().lower() == lowered for synonym in item.get("synonyms", []))
                )
            ]
            if len(filtered) == len(lexicon_payload):
                raise SystemExit(f"Lexicon term not found: {args.term}")
            _write_json(args.lexicon, filtered)
            detector.set_status(args.discovery_file, args.term, "rejected")
            print(f"Removed lexicon entry: {args.term}")
            print(f"Lexicon: {args.lexicon}")
            print(f"Discovery file: {args.discovery_file}")
            return 0

        candidates = detector.load(args.discovery_file)
        candidate = next(
            (
                item
                for item in candidates
                if str(item.get("text", "")).strip().lower() == args.term.strip().lower()
            ),
            None,
        )
        if candidate is None:
            raise SystemExit(f"Candidate not found in discovery file: {args.term}")

        known_terms = {
            str(item.get("canonical_text", "")).strip().lower()
            for item in lexicon_payload
            if str(item.get("canonical_text", "")).strip()
        }
        known_terms.update(
            str(synonym).strip().lower()
            for item in lexicon_payload
            for synonym in item.get("synonyms", [])
            if str(synonym).strip()
        )
        if args.term.strip().lower() in known_terms:
            detector.set_status(args.discovery_file, args.term, "accepted")
            print(f"Already in lexicon: {args.term}")
            return 0

        lexicon_payload.append(
            detector.build_lexicon_entry(candidate, term_type=args.term_type)
        )
        _write_json(args.lexicon, lexicon_payload)
        detector.set_status(args.discovery_file, args.term, "accepted")
        print(f"Added lexicon entry: {args.term}")
        print(f"Lexicon: {args.lexicon}")
        print(f"Discovery file: {args.discovery_file}")
        return 0

    feedback_store = LexiconFeedbackStore(args.feedback_file)
    feedback = feedback_store.aggregate()
    lexicon_payload = _load_json(args.lexicon)
    if not isinstance(lexicon_payload, list):
        raise SystemExit(f"Lexicon file must be a JSON array: {args.lexicon}")
    suggestions = LexiconSuggester().suggest(
        feedback=feedback,
        current_lexicon=lexicon_payload,
        min_feedback_count=args.min_feedback_count,
    )
    _write_json(args.output, suggestions)

    if args.lexicon_command == "suggest":
        print(f"Suggestions: {len(suggestions)}")
        print(f"Output: {args.output}")
        return 0

    if args.lexicon_command == "bump":
        applicable = [
            item
            for item in suggestions
            if abs(float(item.get("net_score", 0.0))) >= 0.30
        ]
        if not args.apply:
            print(f"Applicable suggestions: {len(applicable)}")
            print(f"Preview: {args.output}")
            return 0

        by_term = {item["canonical_text"]: item for item in applicable}
        updated_count = 0
        published_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        for entry in lexicon_payload:
            suggestion = by_term.get(entry.get("canonical_text"))
            if suggestion is None:
                continue
            entry["base_confidence"] = float(suggestion["suggested_base_confidence"])
            learn_stats = entry.setdefault("learn_stats", {})
            if isinstance(learn_stats, dict):
                learn_stats["last_updated"] = published_at
            updated_count += 1
        _write_json(args.lexicon, lexicon_payload)

        release_payload = _load_json(args.release)
        if not isinstance(release_payload, dict):
            raise SystemExit(f"Release file must be a JSON object: {args.release}")
        release_payload["version"] = _bump_release_version(str(release_payload.get("version", "draft")))
        release_payload["published_at"] = published_at
        release_payload["reviewer"] = args.reviewer
        release_payload["change_note"] = f"Applied lexicon confidence bump to {updated_count} entries."
        _write_json(args.release, release_payload)
        print(f"Updated entries: {updated_count}")
        print(f"Lexicon: {args.lexicon}")
        print(f"Release: {args.release}")
        print(f"Suggestions: {args.output}")
        return 0

    raise SystemExit(f"Unsupported lexicon command: {args.lexicon_command}")


def run_cookies(args: argparse.Namespace) -> int:
    if args.cookies_command == "set-weibo":
        target = install_cookie_file(args.cookie_file, target_name="weibo_cookies.json")
        print(f"Weibo cookie installed: {target}")
        return 0
    if args.cookies_command == "set-xueqiu":
        target = install_cookie_file(args.cookie_file, target_name="xueqiu_cookies.json")
        print(f"Xueqiu cookie installed: {target}")
        return 0
    if args.cookies_command != "check":
        raise SystemExit(f"Unsupported cookies command: {args.cookies_command}")

    payload = _load_json(args.sources)
    if not isinstance(payload, dict):
        raise SystemExit(f"Unsupported live source config format: {args.sources}")
    default_client_args = {"user_agent": args.user_agent}
    statuses: list[tuple[str, bool, str]] = []
    weibo_config = payload.get("weibo", {})
    if isinstance(weibo_config, dict):
        collector = WeiboCollector(
            queries=weibo_config.get("queries", ["半导体"]),
            cookie_path=resolve_cookie_path(weibo_config.get("cookie_path", market_news_cookie_dir() / "weibo_cookies.json")),
            http_client=UrllibHttpClient(**default_client_args),
            max_results_per_query=1,
            sleep_range=(0.0, 0.0),
        )
        ok, detail = collector.check_session()
        # 实测结果要落回过期标记，否则看板永远显示上一次的状态：
        # 一次临时失败会把源永久标红，而真坏了的源反而是绿的。
        record_cookie_check(collector.cookie_path, ok, detail)
        statuses.append(("weibo", ok, detail))
    xueqiu_config = payload.get("xueqiu", {})
    if isinstance(xueqiu_config, dict):
        collector = XueqiuCollector(
            queries=xueqiu_config.get("queries", ["半导体"]),
            cookie_path=resolve_cookie_path(xueqiu_config.get("cookie_path", market_news_cookie_dir() / "xueqiu_cookies.json")),
            http_client=UrllibHttpClient(**default_client_args),
            max_results_per_query=1,
            browser_timeout_ms=int(xueqiu_config.get("browser_timeout_ms", 15000)),
            browser_warmup_ms=int(xueqiu_config.get("browser_warmup_ms", 8000)),
        )
        ok, detail = collector.check_session()
        record_cookie_check(collector.cookie_path, ok, detail)
        statuses.append(("xueqiu", ok, detail))

    failed = 0
    for name, ok, detail in statuses:
        state = "ok" if ok else "error"
        print(f"{name}: {state} | {detail}")
        if not ok:
            failed += 1
    return 0 if failed == 0 else 1


def run_news_learning(args: argparse.Namespace) -> int:
    result = _build_news_learning_from_args(args)
    print("News learning artifacts generated.")
    print(f"output_dir: {result.output_dir}")
    for name, path in result.artifact_paths.items():
        print(f"{name}: {path}")
    print(f"candidates: {len(result.candidates)}")
    print(f"review_packet_md: {result.artifact_paths['news_learning_review_packet_md']}")
    print(f"review_packet_json: {result.artifact_paths['news_learning_review_packet_json']}")
    print(f"codex_handoff: {result.artifact_paths['news_learning_codex_handoff']}")
    return 0


def _build_news_learning_from_args(args: argparse.Namespace) -> object:
    return build_news_learning_artifacts(
        report_path=args.report,
        output_dir=args.output_dir,
        min_source_sample=args.min_source_sample,
        min_topic_sample=args.min_topic_sample,
        stale_seconds=args.stale_seconds,
    )


def _copy_text_to_clipboard(text: str) -> tuple[bool, str]:
    try:
        subprocess.run(["pbcopy"], input=text, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        return False, str(exc)
    return True, "copied"


def run_news_learning_export(args: argparse.Namespace) -> int:
    result = _build_news_learning_from_args(args)
    handoff_path = result.artifact_paths["news_learning_codex_handoff"]
    handoff_text = handoff_path.read_text(encoding="utf-8")
    copied = False
    copy_detail = "disabled"
    if bool(getattr(args, "copy_to_clipboard", False)):
        copied, copy_detail = _copy_text_to_clipboard(handoff_text)
    print("News learning Codex handoff exported.")
    print(f"codex_handoff: {handoff_path}")
    print(f"review_packet_md: {result.artifact_paths['news_learning_review_packet_md']}")
    print(f"review_packet_json: {result.artifact_paths['news_learning_review_packet_json']}")
    print(f"candidates: {len(result.candidates)}")
    print(f"clipboard: {'copied' if copied else copy_detail}")
    print("\n这份 handoff 是给 Codex 复核的交接消息；JSON/Markdown 是可校验证据包。")
    return 0


def run_news_learning_status(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    packet_path = output_dir / "news_learning_review_packet.json"
    attribution_path = output_dir / "news_attribution.json"
    candidates_path = output_dir / "news_upgrade_candidates.jsonl"
    promotion_path = output_dir / "news_promotion_report.json"
    handoff_path = output_dir / "news_learning_codex_handoff.md"
    packet = _load_json(packet_path) if packet_path.exists() else {}
    attribution = _load_json(attribution_path) if attribution_path.exists() else {}
    promotion = _load_json(promotion_path) if promotion_path.exists() else {}
    candidates = []
    if candidates_path.exists():
        candidates = [
            json.loads(line)
            for line in candidates_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    rows = [
        ("generated_at", packet.get("generated_at", attribution.get("generated_at", "none")) if isinstance(packet, dict) else "none"),
        ("codex_handoff", handoff_path if handoff_path.exists() else "missing"),
        ("review_packet_md", output_dir / "news_learning_review_packet.md" if (output_dir / "news_learning_review_packet.md").exists() else "missing"),
        ("review_packet_json", packet_path if packet_path.exists() else "missing"),
        ("sample_size", attribution.get("sample_size", 0) if isinstance(attribution, dict) else 0),
        ("candidate_count", len(candidates)),
        (
            "ready_for_codex_review",
            promotion.get("ready_for_codex_review_count", 0) if isinstance(promotion, dict) else 0,
        ),
        ("auto_code_changes_allowed", False),
        ("auto_live_config_changes_allowed", False),
    ]
    for key, value in rows:
        print(f"{key}: {value}")
    if isinstance(attribution, dict):
        best_sources = ", ".join(str(item.get("name")) for item in attribution.get("best_sources", [])[:5])
        best_topics = ", ".join(str(item.get("name")) for item in attribution.get("best_topics", [])[:8])
        print(f"best_sources: {best_sources}")
        print(f"best_topics: {best_topics}")
    return 0


def _write_news_learning_automation_status(
    *,
    status_path: Path,
    history_path: Path,
    payload: dict[str, object],
) -> None:
    status_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def run_news_learning_auto(args: argparse.Namespace) -> int:
    timestamp = utcnow().isoformat()
    try:
        result = _build_news_learning_from_args(args)
        handoff_path = result.artifact_paths["news_learning_codex_handoff"]
        copied = False
        copy_detail = "disabled"
        if bool(getattr(args, "copy_to_clipboard", False)):
            copied, copy_detail = _copy_text_to_clipboard(handoff_path.read_text(encoding="utf-8"))
        promotion_path = result.artifact_paths["news_promotion_report"]
        promotion = _load_json(promotion_path) if promotion_path.exists() else {}
        ready_count = (
            int(promotion.get("ready_for_codex_review_count", 0) or 0)
            if isinstance(promotion, dict)
            else 0
        )
        payload = {
            "timestamp": timestamp,
            "overall_status": "ok",
            "artifacts": {
                "json_report": str(args.report),
                "news_learning_output_dir": str(result.output_dir),
                "news_learning_codex_handoff": str(handoff_path),
                "news_learning_review_packet_md": str(result.artifact_paths["news_learning_review_packet_md"]),
                "news_learning_review_packet_json": str(result.artifact_paths["news_learning_review_packet_json"]),
            },
            "counts": {
                "memory_records": int(result.attribution.get("sample_size", 0) or 0),
                "candidate_count": len(result.candidates),
                "ready_for_codex_review": ready_count,
            },
            "modules": [
                {
                    "name": "news_learning",
                    "status": "ok",
                    "detail": "Codex handoff generated automatically",
                    "count": len(result.candidates),
                }
            ],
            "clipboard": "copied" if copied else copy_detail,
            "errors": [],
        }
        _write_news_learning_automation_status(
            status_path=args.status_file,
            history_path=args.history_file,
            payload=payload,
        )
        print("News learning automation cycle complete.")
        print(f"status: {args.status_file}")
        print(f"codex_handoff: {handoff_path}")
        print(f"candidates: {len(result.candidates)}")
        print(f"ready_for_codex_review: {ready_count}")
        return 0
    except Exception as exc:
        payload = {
            "timestamp": timestamp,
            "overall_status": "error",
            "artifacts": {
                "json_report": str(getattr(args, "report", "")),
                "news_learning_output_dir": str(getattr(args, "output_dir", "")),
            },
            "counts": {
                "memory_records": 0,
                "candidate_count": 0,
                "ready_for_codex_review": 0,
            },
            "modules": [
                {
                    "name": "news_learning",
                    "status": "error",
                    "detail": "automation cycle failed",
                    "count": 0,
                }
            ],
            "errors": [str(exc)],
        }
        _write_news_learning_automation_status(
            status_path=args.status_file,
            history_path=args.history_file,
            payload=payload,
        )
        print(f"News learning automation failed: {exc}", file=sys.stderr)
        return 1


def _resolve_codex_binary(candidate: Path) -> Path:
    if candidate.exists():
        return candidate
    resolved = shutil.which("codex")
    if resolved:
        return Path(resolved)
    raise FileNotFoundError(f"Codex CLI not found: {candidate}")


def _resolve_review_binary(candidate: Path) -> tuple[Path, str]:
    """Pick the reviewer CLI and report which flavour it is.

    Returns ``(path, kind)`` where kind is ``"codex"`` or ``"claude"``. Codex is
    tried first so an existing paid setup keeps working; Claude Code is used when
    Codex is absent or unusable, since the two CLIs need different arguments.
    """

    try:
        return _resolve_codex_binary(candidate), "codex"
    except FileNotFoundError:
        pass
    from market_news.services.model_judgement import _resolve_claude_binary

    claude = _resolve_claude_binary(Path("/opt/homebrew/bin/claude"))
    if claude:
        return Path(claude), "claude"
    raise FileNotFoundError(
        f"No reviewer CLI found (looked for Codex at {candidate}, then claude)."
    )


def _compose_news_learning_codex_review_prompt(
    *,
    handoff_path: Path,
    review_packet_json_path: Path,
    review_packet_md_path: Path,
    analysis_path: Path,
) -> str:
    return f"""
你是 Codex。请只读审阅市场新闻收集系统的 Evidence-to-Review 学习包，判断是否值得建议用户下一步修改代码或采集策略。

工作目录：
`{project_root()}`

请读取：
- `{handoff_path}`
- `{review_packet_json_path}`
- `{review_packet_md_path}`

硬性限制：
- 严禁自动改代码。
- 严禁自动改 live/news production 配置。
- 严禁改股票系统。
- 严禁改 crypto 系统。
- 候选建议只能停留在 research/review 级别。
- 只有当你认为“值得用户下一步下指令让 Codex 改代码或策略”时，才明确提出变更建议。

请重点判断：
- 哪些来源值得升权、降权、拉黑或要求交叉验证。
- 哪些主题真的有预测价值，哪些只是噪声。
- 是否有重复率、滞后率、反驳率、来源单一依赖过高的问题。
- 是否有重要新闻被低质量来源抢先、官方来源滞后、或者抓取链路遗漏。
- 哪些 candidate_id 值得下一步让 Codex 评估或修改。
- 如果 market_impact_after_5m/30m/1d 仍为空，请判断是否已经值得建议接入市场反应标签。

输出要求：
如果没有值得用户采取行动的变更，请只输出：
新闻学习审阅：暂不建议改代码或采集策略。
原因：<一句话说明最主要原因>

如果值得用户判断是否变更，请输出：
新闻学习审阅：建议用户确认是否变更。

最值得看的问题：
1. <问题和证据>
2. <问题和证据>

建议动作：
1. <candidate_id> <action> <target>：<为什么值得做>
2. <candidate_id> <action> <target>：<为什么值得做>

不建议现在做的事：
1. <噪声/样本不足/风险>

如果用户同意，建议下一条指令：
<一句可以直接发给 Codex 的中文指令>

判断标准：
- 样本不足时不要建议改代码，只建议继续观察。
- 单一新闻不能直接证明要改策略，除非它暴露出明确来源缺口或重大漏抓。
- 只有连续出现同类证据时，才建议升权、降权或拉黑来源。
- 微博/雪球只能作为热度或线索，不应单独触发生产策略变更。
- 官方公告、交易所、财联社、新华社、部委、权威财经源优先级更高。
- 如果某主题能更早捕捉基本面变化、订单变化、需求链变化、政策需求或估值修复，优先提出。
- 如果只是事后价格波动、泛泛讨论、没有实体和可验证事实，不要建议变更。

请把最终回复控制在 1200 字以内。最终内容会保存到：
`{analysis_path}`
""".strip()


def _news_learning_review_is_actionable(text: str) -> bool:
    normalized = text.strip()
    if "暂不建议改代码或采集策略" in normalized:
        return False
    return any(
        marker in normalized
        for marker in (
            "建议用户确认是否变更",
            "建议动作",
            "candidate_id",
            "建议下一条指令",
        )
    )


def _news_learning_review_digest(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _news_learning_review_already_sent(text: str, status_path: Path) -> bool:
    """True when this exact analysis was already pushed on a previous run."""

    try:
        if not status_path.exists():
            return False
        previous = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(previous, dict):
        return False
    notification = previous.get("notification")
    if not isinstance(notification, dict) or not notification.get("sent"):
        return False
    return str(notification.get("analysis_digest", "")) == _news_learning_review_digest(text)


def _truncate_notification(text: str, *, limit: int = 3200) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 80].rstrip() + "\n\n...[已截断，完整内容见本地 analysis 文件]"


def run_news_learning_codex_review(args: argparse.Namespace) -> int:
    timestamp = utcnow().isoformat()
    payload: dict[str, object]
    notified = False
    notify_error = ""
    codex_returncode = 1
    analysis_text = ""
    try:
        learning_result = _build_news_learning_from_args(args)
        handoff_path = learning_result.artifact_paths["news_learning_codex_handoff"]
        review_packet_json_path = learning_result.artifact_paths["news_learning_review_packet_json"]
        review_packet_md_path = learning_result.artifact_paths["news_learning_review_packet_md"]
        args.analysis_path.parent.mkdir(parents=True, exist_ok=True)

        prompt = _compose_news_learning_codex_review_prompt(
            handoff_path=handoff_path,
            review_packet_json_path=review_packet_json_path,
            review_packet_md_path=review_packet_md_path,
            analysis_path=args.analysis_path,
        )
        codex_bin, reviewer_kind = _resolve_review_binary(args.codex_bin)
        if reviewer_kind == "claude":
            # Claude Code takes the prompt as an argument and prints to stdout;
            # it has no --output-last-message, so the analysis file is written
            # from stdout further below.
            command = [str(codex_bin), "-p", prompt]
            if args.model:
                command.extend(["--model", str(args.model)])
            stdin_text: str | None = None
        else:
            command = [
                str(codex_bin),
                "-a",
                "never",
                "exec",
                "-C",
                str(project_root()),
                "-s",
                "read-only",
                "--output-last-message",
                str(args.analysis_path),
            ]
            if args.model:
                command.extend(["-m", str(args.model)])
            command.append("-")
            stdin_text = prompt

        completed = subprocess.run(
            command,
            input=stdin_text,
            text=True,
            capture_output=True,
            timeout=int(args.timeout),
            check=False,
        )
        if reviewer_kind == "claude" and completed.returncode == 0:
            args.analysis_path.write_text(
                (completed.stdout or "").strip() + "\n", encoding="utf-8"
            )
        codex_returncode = completed.returncode
        if args.analysis_path.exists():
            analysis_text = args.analysis_path.read_text(encoding="utf-8")
        else:
            analysis_text = completed.stdout.strip() or completed.stderr.strip()
            args.analysis_path.write_text(analysis_text + "\n", encoding="utf-8")

        actionable = _news_learning_review_is_actionable(analysis_text)
        # Guard 1: a failed reviewer must never notify. Previously a non-zero exit
        # (e.g. quota exhausted) left the PREVIOUS run's analysis file on disk, which
        # was then re-read, re-judged "actionable", and re-sent every single hour.
        if codex_returncode != 0:
            actionable = False
        # Guard 2: never send the same analysis twice, even across restarts.
        elif _news_learning_review_already_sent(analysis_text, args.status_file):
            actionable = False
        should_notify = bool(args.notify) and (bool(args.notify_all) or actionable)
        if should_notify:
            message = _truncate_notification(
                "新闻学习审阅自动化\n\n"
                f"{analysis_text.strip()}\n\n"
                f"完整分析：{args.analysis_path}\n"
                f"Review packet：{review_packet_md_path}"
            )
            notifier = OpenClawNotifier(
                binary_path=args.openclaw_bin,
                config_path=args.openclaw_config,
            )
            target = notifier.resolve_target(args.channel, args.target)
            try:
                notifier.send(channel=args.channel, target=target, message=message)
                notified = True
            except Exception as exc:
                notify_error = str(exc)

        overall_status = "ok" if codex_returncode == 0 and not notify_error else "degraded"
        payload = {
            "timestamp": timestamp,
            "overall_status": overall_status,
            "artifacts": {
                "analysis": str(args.analysis_path),
                "news_learning_codex_handoff": str(handoff_path),
                "news_learning_review_packet_md": str(review_packet_md_path),
                "news_learning_review_packet_json": str(review_packet_json_path),
            },
            "counts": {
                "candidate_count": len(learning_result.candidates),
                "ready_for_codex_review": int(
                    _load_json(learning_result.artifact_paths["news_promotion_report"]).get(
                        "ready_for_codex_review_count", 0
                    )
                ),
            },
            "modules": [
                {
                    "name": "news_learning_codex_review",
                    "status": overall_status,
                    "detail": "Codex reviewed the news learning packet",
                    "count": len(learning_result.candidates),
                }
            ],
            "codex": {
                "returncode": codex_returncode,
                "model": str(args.model or "default"),
                "reviewer": reviewer_kind,
                "binary": str(codex_bin),
            },
            "notification": {
                "attempted": should_notify,
                "sent": notified,
                "error": notify_error,
                "analysis_digest": _news_learning_review_digest(analysis_text),
            },
            "actionable": actionable,
            "errors": [] if codex_returncode == 0 else [completed.stderr.strip() or completed.stdout.strip()],
        }
        _write_news_learning_automation_status(
            status_path=args.status_file,
            history_path=args.history_file,
            payload=payload,
        )
        print("News learning Codex review complete.")
        print(f"analysis: {args.analysis_path}")
        print(f"actionable: {actionable}")
        print(f"notified: {notified}")
        if notify_error:
            print(f"notification_error: {notify_error}", file=sys.stderr)
        return 0 if codex_returncode == 0 and not notify_error else 1
    except Exception as exc:
        payload = {
            "timestamp": timestamp,
            "overall_status": "error",
            "artifacts": {
                "analysis": str(getattr(args, "analysis_path", "")),
                "news_learning_output_dir": str(getattr(args, "output_dir", "")),
            },
            "counts": {
                "candidate_count": 0,
                "ready_for_codex_review": 0,
            },
            "modules": [
                {
                    "name": "news_learning_codex_review",
                    "status": "error",
                    "detail": "Codex review automation failed",
                    "count": 0,
                }
            ],
            "notification": {
                "attempted": False,
                "sent": False,
                "error": "",
            },
            "actionable": False,
            "errors": [str(exc)],
        }
        _write_news_learning_automation_status(
            status_path=args.status_file,
            history_path=args.history_file,
            payload=payload,
        )
        print(f"News learning Codex review failed: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return run_pipeline(args)
    if args.command == "live":
        return run_live_pipeline(args)
    if args.command == "collect":
        return run_collect(args)
    if args.command == "dashboard":
        return run_dashboard(args)
    if args.command == "notify":
        return run_notify(args)
    if args.command == "probe":
        return run_probe(args)
    if args.command == "health":
        return run_health(args)
    if args.command == "review-api":
        return run_review_api(args)
    if args.command == "lexicon":
        return run_lexicon(args)
    if args.command == "cookies":
        return run_cookies(args)
    if args.command in {"news-learning", "news-learning-build"}:
        return run_news_learning(args)
    if args.command == "news-learning-export":
        return run_news_learning_export(args)
    if args.command == "news-learning-status":
        return run_news_learning_status(args)
    if args.command == "news-learning-auto":
        return run_news_learning_auto(args)
    if args.command == "news-learning-codex-review":
        return run_news_learning_codex_review(args)
    if args.command == "monitor":
        return run_monitor(args)
    if args.command == "ah-scan":
        return run_ah_scan(args)
    parser.error(f"Unsupported command: {args.command}")
    return 2


def run_ah_scan(args: argparse.Namespace) -> int:
    """Driver for the AH multi-factor scanner. Best-effort; always exits 0
    unless something truly catastrophic happens, so launchd / cron retries
    don't go into back-off."""

    from market_news.services import ah_scanner

    markets_raw = getattr(args, "markets", None) or "HK,SH,SZ"
    markets = tuple(
        part.strip().upper() for part in markets_raw.split(",") if part.strip()
    ) or ("HK", "SH", "SZ")
    cfg = ah_scanner.ScannerConfig.from_env()
    cfg = ah_scanner.ScannerConfig(
        markets=markets,
        top_n=int(getattr(args, "top", cfg.top_n) or cfg.top_n),
        volume_shrink_ratio=cfg.volume_shrink_ratio,
        volume_lookback_days=cfg.volume_lookback_days,
        limit_up_lookback_days=cfg.limit_up_lookback_days,
        ath_pct=cfg.ath_pct,
        ath_lookback_days=cfg.ath_lookback_days,
        history_per_call_cap=cfg.history_per_call_cap,
        universe_size_cap=cfg.universe_size_cap,
    )
    try:
        report = ah_scanner.run_scan(cfg)
    except Exception as exc:
        print(f"[ah-scan] scan failed: {exc}")
        return 0
    reports_dir = project_root() / "reports" / "live"
    paths = ah_scanner.write_reports(report, reports_dir=reports_dir)
    print(f"[ah-scan] wrote {len(paths)} files to {reports_dir}")
    print(f"  candidates: {report.candidate_count}, universe: {len(report.universe)}")
    print(
        f"  limit_up_streak: {len(report.limit_up_streak)}, "
        f"volume_shrink_up: {len(report.volume_shrink_up)}, "
        f"near_ath: {len(report.near_ath)}"
    )
    if getattr(args, "update_universe", False):
        config_dir = project_root() / "config"
        dyn_path = ah_scanner.write_dynamic_universe(report, config_dir=config_dir)
        if dyn_path is not None:
            print(f"[ah-scan] wrote dynamic universe -> {dyn_path}")
            print("[ah-scan] note: pipeline still uses the static universe unless "
                  "MARKET_NEWS_TECH_UNIVERSE_DYNAMIC=1 is set.")
        else:
            print("[ah-scan] dynamic universe was empty — static file remains in use.")
    if report.skipped_reason:
        print(f"[ah-scan] skipped: {report.skipped_reason}")
    if report.errors:
        print(f"[ah-scan] {len(report.errors)} non-fatal errors during scan (see scan_summary.json).")
    return 0
