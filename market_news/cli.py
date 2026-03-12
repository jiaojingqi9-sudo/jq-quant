from __future__ import annotations

import argparse
import os
from pathlib import Path
import time

from market_news.application.monitoring import MonitorStateWriter
from market_news.application.notify import NotificationResult, NotificationRunner
from market_news.application.pipeline import MarketNewsPipeline
from market_news.dashboard import render_dashboard, watch_dashboard
from market_news.domain.models import AlertLevel
from market_news.infrastructure.collectors.factory import build_live_collector
from market_news.infrastructure.collectors.local_json import LocalJSONCollector
from market_news.infrastructure.http import default_user_agent
from market_news.infrastructure.notifications.openclaw import OpenClawNotifier
from market_news.infrastructure.persistence.sqlite_store import SQLiteRunStore
from market_news.services.alerts import RuleBasedAlertEngine
from market_news.services.clustering import KeywordEventClusterer
from market_news.services.deduplication import FingerprintDeduplicator
from market_news.services.impact import ConfigDrivenImpactAnalyzer
from market_news.services.mapping import ConfigDrivenInstrumentMapper
from market_news.services.normalization import DefaultNormalizer
from market_news.services.ranking import WeightedEventRanker, WeightedInstrumentRanker
from market_news.services.reporting import MarkdownJsonReporter


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
        reporter=MarkdownJsonReporter(report_dir, top_n=top),
    )


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
    _print_snapshot(snapshot)
    return 0


def execute_live_pipeline(args: argparse.Namespace) -> object:
    pipeline = build_pipeline(
        collector=build_live_collector(args.sources, args.user_agent),
        rules=args.rules,
        universe=args.universe,
        database=args.database,
        report_dir=args.report_dir,
        top=args.top,
    )
    return pipeline.run()


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


def run_notify(args: argparse.Namespace) -> int:
    runner = _build_notification_runner(args)

    def cycle() -> int:
        if args.refresh:
            snapshot = execute_live_pipeline(args)
            args.report = Path(snapshot.artifacts["json_report"])
        args.preview = _resolve_preview_path(args.preview, args.report)
        result, exit_code = _deliver_notification(
            args,
            runner,
            report_path=args.report,
            preview_path=args.preview,
            dry_run=args.dry_run,
        )
        print(f"Status: {result.status}")
        print(f"Channel: {result.channel}")
        print(f"Target: {result.target}")
        print(f"Alerts: {result.alert_count}")
        print(f"Preview: {result.preview_path}")
        print(result.detail)
        return exit_code

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
            os.system("clear")
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
            os.system("clear")
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return run_pipeline(args)
    if args.command == "live":
        return run_live_pipeline(args)
    if args.command == "dashboard":
        return run_dashboard(args)
    if args.command == "notify":
        return run_notify(args)
    if args.command == "probe":
        return run_probe(args)
    if args.command == "monitor":
        return run_monitor(args)
    parser.error(f"Unsupported command: {args.command}")
    return 2
