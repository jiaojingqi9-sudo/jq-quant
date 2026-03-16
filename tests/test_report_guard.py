from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest

from market_news.cli import _backup_report_bundle, _report_has_content, _restore_report_bundle


class ReportGuardTest(unittest.TestCase):
    def test_backup_and_restore_keep_last_non_empty_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report_dir = Path(temp_dir)
            report_path = report_dir / "latest_report.json"
            markdown_path = report_dir / "latest_report.md"
            html_path = report_dir / "latest_dashboard.html"

            report_path.write_text(
                json.dumps(
                    {
                        "counts": {"raw_records": 10, "documents": 8, "clusters": 6, "ranked_events": 4},
                        "latest_feed": [{"title": "foo"}],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            markdown_path.write_text("old markdown", encoding="utf-8")
            html_path.write_text("old html", encoding="utf-8")

            self.assertTrue(_report_has_content(report_path))
            bundle = _backup_report_bundle(report_dir)
            self.assertIn("latest_report.json", bundle)

            report_path.write_text("{}", encoding="utf-8")
            markdown_path.write_text("new markdown", encoding="utf-8")
            html_path.write_text("new html", encoding="utf-8")

            _restore_report_bundle(report_dir, bundle)

            self.assertIn("foo", report_path.read_text(encoding="utf-8"))
            self.assertEqual(markdown_path.read_text(encoding="utf-8"), "old markdown")
            self.assertEqual(html_path.read_text(encoding="utf-8"), "old html")


if __name__ == "__main__":
    unittest.main()
