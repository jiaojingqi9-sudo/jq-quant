from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path

from market_news.domain.models import AlertLevel
from market_news.domain.ports import DeliveryStore, Notifier
from market_news.services.notification import AlertDigestBuilder


_log = logging.getLogger(__name__)


def _futu_enrichment_enabled() -> bool:
    raw = os.getenv("MARKET_NEWS_FUTU_ENRICHMENT", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _maybe_write_futu_enrichment(payload: dict, preview_path: Path) -> None:
    """Sidecar write — best effort, never raises.

    Reads ``MARKET_NEWS_FUTU_ENRICHMENT``. When the flag is off, returns
    immediately so the notify path keeps its current behavior bit-for-bit.
    When the flag is on, asks the futu_enrichment service for extra context
    and writes a sidecar JSON next to the preview file. The phone-alert
    preview text and the WhatsApp send are untouched.
    """

    if not _futu_enrichment_enabled():
        return
    try:
        from market_news.services import futu_enrichment

        report = futu_enrichment.build_report(payload, enabled=True)
        futu_enrichment.write_sidecar(report, preview_path=preview_path)
    except Exception as exc:  # never break notify
        _log.warning("futu enrichment sidecar failed (notify continues normally): %s", exc)


@dataclass(slots=True)
class NotificationResult:
    status: str
    channel: str
    target: str
    alert_count: int
    preview_path: Path
    cluster_ids: list[str]
    detail: str
    modules: list[dict[str, object]] = field(default_factory=list)


class NotificationRunner:
    def __init__(
        self,
        *,
        store: DeliveryStore,
        notifier: Notifier,
    ) -> None:
        self.store = store
        self.notifier = notifier

    def deliver_from_report(
        self,
        *,
        report_path: Path,
        preview_path: Path,
        channel: str,
        target: str | None = None,
        min_level: AlertLevel = AlertLevel.HIGH,
        max_alerts: int = 3,
        include_existing: bool = False,
        force: bool = False,
        dry_run: bool = False,
    ) -> NotificationResult:
        if not report_path.exists():
            raise FileNotFoundError(
                f"Report not found: {report_path}. Run `python3 -m market_news live` first."
            )

        payload = json.loads(report_path.read_text(encoding="utf-8"))
        resolved_target = self.notifier.resolve_target(channel, target)
        sent_cluster_ids = set()
        if not force:
            sent_cluster_ids = self.store.load_sent_alert_cluster_ids(channel, resolved_target)

        builder = AlertDigestBuilder(
            min_level=min_level,
            max_alerts=max_alerts,
            include_existing=include_existing,
        )
        plan = builder.compose(
            payload,
            channel=channel,
            target=resolved_target,
            sent_cluster_ids=sent_cluster_ids,
            preview_path=preview_path,
        )
        if plan is None:
            preview_path.parent.mkdir(parents=True, exist_ok=True)
            message = "当前没有新的高优先级提醒可发送。"
            preview_path.write_text(message + "\n", encoding="utf-8")
            return NotificationResult(
                status="skipped",
                channel=channel,
                target=resolved_target,
                alert_count=0,
                preview_path=preview_path,
                cluster_ids=[],
                detail=message,
                modules=[
                    {
                        "name": "core_alerts",
                        "status": "idle",
                        "count": 0,
                        "detail": "AI fully screened high/critical alert stream",
                    },
                    {
                        "name": "tech_block",
                        "status": "idle",
                        "count": 0,
                        "detail": "AI fully screened A/H tech catalyst stream",
                    },
                ],
            )

        plan.preview_path.parent.mkdir(parents=True, exist_ok=True)
        plan.preview_path.write_text(plan.message + "\n", encoding="utf-8")
        _maybe_write_futu_enrichment(payload, plan.preview_path)

        if dry_run:
            return NotificationResult(
                status="preview",
                channel=plan.channel,
                target=plan.target,
                alert_count=plan.alert_count,
                preview_path=plan.preview_path,
                cluster_ids=plan.cluster_ids,
                detail="Preview generated only; nothing sent.",
                modules=plan.modules,
            )

        try:
            transport_detail = self.notifier.send(
                channel=plan.channel,
                target=plan.target,
                message=plan.message,
            )
        except Exception as exc:
            return NotificationResult(
                status="preview",
                channel=plan.channel,
                target=plan.target,
                alert_count=plan.alert_count,
                preview_path=plan.preview_path,
                cluster_ids=plan.cluster_ids,
                detail=(
                    "Phone delivery is temporarily unavailable; the alert preview was kept locally. "
                    f"{exc}"
                ),
                modules=plan.modules,
            )
        run_id = str(payload.get("run_id", "")).strip() or "unknown-run"
        self.store.persist_alert_delivery(
            run_id=run_id,
            channel=plan.channel,
            target=plan.target,
            cluster_ids=plan.cluster_ids,
            message_text=plan.message,
        )
        return NotificationResult(
            status="sent",
            channel=plan.channel,
            target=plan.target,
            alert_count=plan.alert_count,
            preview_path=plan.preview_path,
            cluster_ids=plan.cluster_ids,
            detail=transport_detail,
            modules=plan.modules,
        )

    def send_probe(
        self,
        *,
        channel: str,
        target: str | None = None,
        preview_path: Path,
        message: str | None = None,
        dry_run: bool = False,
        status_path: Path | None = None,
    ) -> NotificationResult:
        resolved_target = self.notifier.resolve_target(channel, target)
        probe_message = message or self._build_probe_message(status_path=status_path)
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_text(probe_message + "\n", encoding="utf-8")

        if dry_run:
            return NotificationResult(
                status="preview",
                channel=channel,
                target=resolved_target,
                alert_count=1,
                preview_path=preview_path,
                cluster_ids=[],
                detail="Probe preview generated only; nothing sent.",
                modules=[],
            )

        transport_detail = self.notifier.send(
            channel=channel,
            target=resolved_target,
            message=probe_message,
        )
        return NotificationResult(
            status="sent",
            channel=channel,
            target=resolved_target,
            alert_count=1,
            preview_path=preview_path,
            cluster_ids=[],
            detail=transport_detail,
            modules=[],
        )

    def _build_probe_message(self, *, status_path: Path | None = None) -> str:
        now = datetime.now(UTC).isoformat()
        lines = [
            "市场新闻系统测试消息",
            f"时间: {now}",
            "模式: probe",
            "说明: 这是一条人工测试消息，用于验证 OpenClaw -> 手机提醒链路。",
        ]

        if status_path is not None and status_path.exists():
            try:
                payload = json.loads(status_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                counts = payload.get("counts", {})
                alert_counts = payload.get("alert_counts", {})
                lines.extend(
                    [
                        "",
                        "最近运行状态:",
                        f"- overall_status: {payload.get('overall_status', 'n/a')}",
                        f"- events: {counts.get('ranked_events', 0)} | instruments: {counts.get('ranked_instruments', 0)}",
                        f"- alerts: critical={alert_counts.get('critical', 0)} high={alert_counts.get('high', 0)} medium={alert_counts.get('medium', 0)}",
                    ]
                )

        lines.extend(
            [
                "",
                "如果你收到这条消息，说明手机通知链路是通的。",
            ]
        )
        return "\n".join(lines)
