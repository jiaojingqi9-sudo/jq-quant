from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from market_news.application.notify import NotificationResult
from market_news.common import utcnow
from market_news.domain.models import PipelineSnapshot


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
        }

    def _resolve_overall_status(self, result: NotificationResult | None) -> str:
        if result is None:
            return "ok"
        if result.status == "error":
            return "degraded"
        return "ok"

    def _write(self, payload: dict[str, object]) -> None:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.status_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
