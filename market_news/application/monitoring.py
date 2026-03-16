from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from market_news.application.notify import NotificationResult
from market_news.common import utcnow
from market_news.domain.models import PipelineSnapshot


def _write_status(status_path: Path, history_path: Path, payload: dict[str, object]) -> None:
    """Write a status payload to both the latest-status file and the append-only history log."""
    status_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


@dataclass(slots=True)
class MonitorStateWriter:
    status_path: Path
    history_path: Path

    def write_cycle(
        self,
        *,
        snapshot: PipelineSnapshot,
        report_path: Path,
        preview_path: Path,
        notification_result: NotificationResult | None,
    ) -> dict[str, object]:
        payload = {
            "timestamp": utcnow().isoformat(),
            "overall_status": self._resolve_overall_status(notification_result),
            "run_id": snapshot.run_id,
            "source": snapshot.source_name,
            "counts": {
                "raw_records": len(snapshot.raw_records),
                "documents": len(snapshot.documents),
                "clusters": len(snapshot.clusters),
                "ranked_events": len(snapshot.ranked_events),
                "ranked_instruments": len(snapshot.ranked_instruments),
                "alerts": len(snapshot.alerts),
            },
            "alert_counts": {
                "critical": sum(1 for item in snapshot.alerts if item.level.value == "critical"),
                "high": sum(1 for item in snapshot.alerts if item.level.value == "high"),
                "medium": sum(1 for item in snapshot.alerts if item.level.value == "medium"),
                "new": sum(1 for item in snapshot.alerts if item.is_new),
            },
            "artifacts": {
                "json_report": str(report_path),
                "markdown_report": snapshot.artifacts.get("markdown_report", ""),
                "phone_preview": str(preview_path),
            },
            "modules": self._snapshot_modules(snapshot),
            "notification": self._notification_payload(notification_result),
            "errors": [],
        }
        self._write(payload)
        return payload

    def write_failure(
        self,
        *,
        error_message: str,
        report_path: Path | None = None,
        preview_path: Path | None = None,
        notification_result: NotificationResult | None = None,
    ) -> dict[str, object]:
        payload = {
            "timestamp": utcnow().isoformat(),
            "overall_status": "error",
            "run_id": None,
            "source": None,
            "counts": {
                "raw_records": 0,
                "documents": 0,
                "clusters": 0,
                "ranked_events": 0,
                "ranked_instruments": 0,
                "alerts": 0,
            },
            "alert_counts": {
                "critical": 0,
                "high": 0,
                "medium": 0,
                "new": 0,
            },
            "artifacts": {
                "json_report": str(report_path) if report_path else "",
                "markdown_report": "",
                "phone_preview": str(preview_path) if preview_path else "",
            },
            "modules": [
                {"name": "core_market", "status": "error", "detail": "collection cycle failed"},
                {"name": "tech_block", "status": "error", "detail": "collection cycle failed"},
                {"name": "lexicon_discovery", "status": "error", "detail": "collection cycle failed"},
            ],
            "notification": self._notification_payload(notification_result),
            "errors": [error_message],
        }
        self._write(payload)
        return payload

    def _notification_payload(
        self,
        result: NotificationResult | None,
    ) -> dict[str, object] | None:
        if result is None:
            return None
        return {
            "status": result.status,
            "channel": result.channel,
            "target": result.target,
            "alert_count": result.alert_count,
            "preview_path": str(result.preview_path),
            "cluster_ids": result.cluster_ids,
            "detail": result.detail,
            "modules": result.modules,
        }

    def _resolve_overall_status(self, result: NotificationResult | None) -> str:
        if result is None:
            return "ok"
        if result.status == "error":
            return "degraded"
        return "ok"

    def _snapshot_modules(self, snapshot: PipelineSnapshot) -> list[dict[str, object]]:
        tech_block = snapshot.feature_blocks.get("tech_block", {})
        discovery_block = snapshot.feature_blocks.get("lexicon_discovery", {})
        tech_summary = tech_block.get("summary", {}) if isinstance(tech_block, dict) else {}
        tech_signals = tech_block.get("signals", []) if isinstance(tech_block, dict) else []
        tech_assets = tech_block.get("asset_ladder", []) if isinstance(tech_block, dict) else []
        discovery_summary = discovery_block if isinstance(discovery_block, dict) else {}
        if isinstance(tech_summary, dict):
            signal_count = int(tech_summary.get("signal_count", 0) or 0)
            theme_count = int(tech_summary.get("hot_theme_count", 0) or 0)
        else:
            signal_count = 0
            theme_count = 0
        modules = [
            {
                "name": "core_market",
                "status": "ok",
                "detail": "global market ranking chain is active",
                "event_count": len(snapshot.ranked_events),
                "instrument_count": len(snapshot.ranked_instruments),
                "alert_count": len(snapshot.alerts),
            },
            {
                "name": "tech_block",
                "status": "ok" if "tech_block" in snapshot.feature_blocks else "missing",
                "detail": "A/H tech catalyst block is active"
                if "tech_block" in snapshot.feature_blocks
                else "A/H tech catalyst block did not run",
                "signal_count": signal_count,
                "theme_count": theme_count,
                "asset_count": len(tech_assets) if isinstance(tech_assets, list) else 0,
                "active_signal_count": len(tech_signals) if isinstance(tech_signals, list) else 0,
            },
        ]
        if discovery_summary:
            pending_count = int(discovery_summary.get("pending_count", 0) or 0)
            saved_count = int(discovery_summary.get("saved_count", 0) or 0)
            modules.append(
                {
                    "name": "lexicon_discovery",
                    "status": "ok" if discovery_summary.get("enabled", True) else "idle",
                    "detail": "unknown-term discovery queue is tracking new tech vocabulary",
                    "pending_count": pending_count,
                    "saved_count": saved_count,
                }
            )
        return modules

    def _write(self, payload: dict[str, object]) -> None:
        _write_status(self.status_path, self.history_path, payload)


@dataclass(slots=True)
class DeliveryStateWriter:
    status_path: Path
    history_path: Path

    def write_cycle(
        self,
        *,
        report_path: Path,
        preview_path: Path,
        notification_result: NotificationResult,
    ) -> dict[str, object]:
        payload = {
            "timestamp": utcnow().isoformat(),
            "overall_status": self._resolve_overall_status(notification_result),
            "artifacts": {
                "json_report": str(report_path),
                "phone_preview": str(preview_path),
            },
            "modules": notification_result.modules,
            "notification": {
                "status": notification_result.status,
                "channel": notification_result.channel,
                "target": notification_result.target,
                "alert_count": notification_result.alert_count,
                "preview_path": str(notification_result.preview_path),
                "cluster_ids": notification_result.cluster_ids,
                "detail": notification_result.detail,
                "modules": notification_result.modules,
            },
            "errors": [],
        }
        self._write(payload)
        return payload

    def write_failure(
        self,
        *,
        error_message: str,
        report_path: Path | None = None,
        preview_path: Path | None = None,
        notification_result: NotificationResult | None = None,
    ) -> dict[str, object]:
        payload = {
            "timestamp": utcnow().isoformat(),
            "overall_status": "error",
            "artifacts": {
                "json_report": str(report_path) if report_path else "",
                "phone_preview": str(preview_path) if preview_path else "",
            },
            "modules": [
                {"name": "core_alerts", "status": "error", "detail": "delivery cycle failed"},
                {"name": "tech_block", "status": "error", "detail": "delivery cycle failed"},
            ],
            "notification": None
            if notification_result is None
            else {
                "status": notification_result.status,
                "channel": notification_result.channel,
                "target": notification_result.target,
                "alert_count": notification_result.alert_count,
                "preview_path": str(notification_result.preview_path),
                "cluster_ids": notification_result.cluster_ids,
                "detail": notification_result.detail,
                "modules": notification_result.modules,
            },
            "errors": [error_message],
        }
        self._write(payload)
        return payload

    def _resolve_overall_status(self, result: NotificationResult) -> str:
        if result.status == "error":
            return "error"
        if result.status == "skipped":
            return "idle"
        return "ok"

    def _write(self, payload: dict[str, object]) -> None:
        _write_status(self.status_path, self.history_path, payload)
