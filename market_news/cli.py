from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
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
from market_news.application.review_api import LexiconReviewService, ReviewApiStateWriter, serve_review_api
from market_news.dashboard import render_dashboard, watch_dashboard
from market_news.domain.models import AlertLevel
from market_news.infrastructure.collectors.factory import build_live_collector
from market_news.infrastructure.collectors.weibo import WeiboCollector
from market_news.infrastructure.collectors.xueqiu import XueqiuCollector
from market_news.infrastructure.collectors.local_json import LocalJSONCollector
from market_news.infrastructure.cookie_store import install_cookie_file, market_news_cookie_dir, resolve_cookie_path
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
    tech_block = AHShareTechFeatureBlock.from_files(
        universe_path=project_root() / "config" / "tech_universe_cn_hk.json",
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
        impact_analyzer=ConfigDrivenImpactAnalyzer.from_file(rules),
        event_ranker=WeightedEventRanker(),
        instrument_mapper=ConfigDrivenInstrumentMapper.from_file(universe),
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
        if previous_report_bundle:
            _restore_report_bundle(args.report_dir, previous_report_bundle)
        raise RuntimeError(
            "Live collection returned 0 records, so the last non-empty report was preserved instead of blanking the dashboard."
        )
    return snapshot


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
                snapshot = execute_live_pipeline(args)
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
        try:
            snapshot = execute_live_pipeline(args)
            report_path = Path(snapshot.artifacts["json_report"])
            preview_path = _resolve_preview_path(args.preview, report_path)
            state_writer.write_cycle(
                snapshot=snapshot,
                report_path=report_path,
                preview_path=preview_path,
                notification_result=None,
            )
            refresh_runtime_status_views(report_path)
            _clear_console()
            print(
                _render_collection_screen(
                    report_path=report_path,
                    top_n=args.top,
                    status_path=state_writer.status_path,
                )
            )
            return 0
        except Exception as exc:
            state_writer.write_failure(
                error_message=str(exc),
                preview_path=preview_path,
            )
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
                ("review_api", default_review_api_status_path()),
                ("monitor", default_monitor_status_path()),
            ]
        )

    def cycle() -> int:
        snapshot = evaluate_status_files(
            status_files=resolve_inputs(),
            max_age_seconds=args.max_age,
        )
        state_writer.write(snapshot)
        refresh_runtime_status_views(default_live_report_path())
        _clear_console()
        print(render_health_screen(snapshot, status_path=state_writer.status_path))
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
            snapshot = execute_live_pipeline(args)
            report_path = Path(snapshot.artifacts["json_report"])
            preview_path = _resolve_preview_path(args.preview, report_path)
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
            )
            _clear_console()
            print(
                _render_monitor_screen(
                    report_path=report_path,
                    top_n=args.top,
                    notification_result=notification_result,
                    status_path=state_writer.status_path,
                )
            )
            return exit_code
        except Exception as exc:
            state_writer.write_failure(
                error_message=str(exc),
                report_path=report_path,
                preview_path=preview_path,
                notification_result=notification_result,
            )
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
        statuses.append(("weibo", *collector.check_session()))
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
        statuses.append(("xueqiu", *collector.check_session()))

    failed = 0
    for name, ok, detail in statuses:
        state = "ok" if ok else "error"
        print(f"{name}: {state} | {detail}")
        if not ok:
            failed += 1
    return 0 if failed == 0 else 1


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
    if args.command == "monitor":
        return run_monitor(args)
    parser.error(f"Unsupported command: {args.command}")
    return 2
