from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest

from market_news.application.health import (
    HealthStateWriter,
    discover_status_files,
    evaluate_status_files,
    exit_code_for,
)
from market_news.common import utcnow


class HealthMonitorTest(unittest.TestCase):
    def test_health_reports_ok_for_fresh_status_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            report_path = temp_path / "reports" / "latest_report.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text("{}", encoding="utf-8")
            status_path = temp_path / "reports" / "collect_status.json"
            status_path.write_text(
                json.dumps(
                    {
                        "timestamp": utcnow().isoformat(),
                        "overall_status": "ok",
                        "artifacts": {"json_report": str(report_path)},
                        "errors": [],
                    }
                ),
                encoding="utf-8",
            )

            snapshot = evaluate_status_files(
                status_files=[("collect", status_path)],
                max_age_seconds=900,
            )

            self.assertEqual(snapshot.overall_status, "ok")
            self.assertEqual(snapshot.checks[0].status, "ok")
            self.assertEqual(exit_code_for(snapshot), 0)

    def test_health_reports_stale_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            stale_status = temp_path / "reports" / "delivery_status.json"
            stale_status.parent.mkdir(parents=True, exist_ok=True)
            stale_status.write_text(
                json.dumps(
                    {
                        "timestamp": "2020-01-01T00:00:00+00:00",
                        "overall_status": "ok",
                        "artifacts": {"json_report": ""},
                        "errors": [],
                    }
                ),
                encoding="utf-8",
            )

            snapshot = evaluate_status_files(
                status_files=[
                    ("delivery", stale_status),
                    ("collect", temp_path / "reports" / "collect_status.json"),
                ],
                max_age_seconds=60,
            )

            self.assertEqual(snapshot.overall_status, "error")
            statuses = {check.name: check.status for check in snapshot.checks}
            self.assertEqual(statuses["delivery"], "stale")
            self.assertEqual(statuses["collect"], "missing")
            self.assertEqual(exit_code_for(snapshot), 2)

    def test_health_reports_degraded_when_feature_module_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            report_path = temp_path / "reports" / "latest_report.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text("{}", encoding="utf-8")
            status_path = temp_path / "reports" / "collect_status.json"
            status_path.write_text(
                json.dumps(
                    {
                        "timestamp": utcnow().isoformat(),
                        "overall_status": "ok",
                        "artifacts": {"json_report": str(report_path)},
                        "modules": [
                            {"name": "core_market", "status": "ok", "detail": "alive"},
                            {"name": "tech_block", "status": "missing", "detail": "not wired"},
                        ],
                        "errors": [],
                    }
                ),
                encoding="utf-8",
            )

            snapshot = evaluate_status_files(
                status_files=[("collect", status_path)],
                max_age_seconds=900,
            )

            self.assertEqual(snapshot.overall_status, "degraded")
            self.assertEqual(snapshot.checks[0].status, "degraded")
            self.assertEqual(snapshot.checks[0].modules[1]["name"], "tech_block")

    def test_health_keeps_delivery_healthy_after_preview_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            report_path = temp_path / "reports" / "latest_report.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text("{}", encoding="utf-8")
            delivery_status = temp_path / "reports" / "delivery_status.json"
            delivery_status.write_text(
                json.dumps(
                    {
                        "timestamp": utcnow().isoformat(),
                        "overall_status": "ok",
                        "artifacts": {"json_report": str(report_path)},
                        "modules": [
                            {"name": "core_alerts", "status": "active", "detail": "preview kept locally"},
                            {"name": "tech_block", "status": "active", "detail": "preview kept locally"},
                        ],
                        "errors": [],
                    }
                ),
                encoding="utf-8",
            )

            snapshot = evaluate_status_files(
                status_files=[("delivery", delivery_status)],
                max_age_seconds=900,
            )

            self.assertEqual(snapshot.overall_status, "ok")
            self.assertEqual(snapshot.checks[0].status, "ok")

    def test_writer_persists_health_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            status_path = temp_path / "reports" / "health_status.json"
            history_path = temp_path / "reports" / "health_history.jsonl"
            writer = HealthStateWriter(
                status_path=status_path,
                history_path=history_path,
            )
            snapshot = evaluate_status_files(
                status_files=[("monitor", temp_path / "reports" / "monitor_status.json")],
                max_age_seconds=60,
            )

            payload = writer.write(snapshot)

            self.assertEqual(payload["overall_status"], "error")
            saved_status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_status["checks"][0]["status"], "missing")
            self.assertEqual(len(history_path.read_text(encoding="utf-8").strip().splitlines()), 1)

    def test_discovery_prefers_isolated_lines_over_legacy_monitor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            collect_status = temp_path / "collect_status.json"
            delivery_status = temp_path / "delivery_status.json"
            monitor_status = temp_path / "monitor_status.json"
            collect_status.write_text("{}", encoding="utf-8")
            delivery_status.write_text("{}", encoding="utf-8")
            monitor_status.write_text("{}", encoding="utf-8")

            resolved = discover_status_files(
                [
                    ("collect", collect_status),
                    ("delivery", delivery_status),
                    ("monitor", monitor_status),
                ]
            )

            self.assertEqual(resolved, [("collect", collect_status), ("delivery", delivery_status)])


if __name__ == "__main__":
    unittest.main()
