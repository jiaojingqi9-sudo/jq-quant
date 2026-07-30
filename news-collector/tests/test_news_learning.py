from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest

from market_news.cli import (
    _attach_news_learning_artifacts,
    build_parser,
    run_news_learning,
    run_news_learning_auto,
    run_news_learning_codex_review,
    run_news_learning_export,
    run_news_learning_status,
)
from market_news.services.news_learning import build_news_learning_artifacts


def _jsonl_rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class NewsLearningTest(unittest.TestCase):
    def test_builds_research_only_review_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report_path = root / "latest_report.json"
            output_dir = root / "news_learning"
            self._write_sample_report(report_path)

            result = build_news_learning_artifacts(report_path=report_path, output_dir=output_dir)

            expected_names = {
                "news_memory",
                "news_claims",
                "news_outcomes",
                "news_attribution",
                "news_upgrade_candidates",
                "news_promotion_report",
                "news_learning_review_packet_json",
                "news_learning_review_packet_md",
                "news_learning_codex_handoff",
            }
            self.assertEqual(set(result.artifact_paths), expected_names)
            for path in result.artifact_paths.values():
                self.assertTrue(path.exists(), path)
                self.assertGreater(path.stat().st_size, 0, path)

            memory = _jsonl_rows(result.artifact_paths["news_memory"])
            self.assertGreaterEqual(len(memory), 3)
            self.assertTrue(
                {
                    "source",
                    "url",
                    "title",
                    "published_at",
                    "fetched_at",
                    "symbols",
                    "entities",
                    "topics",
                    "raw_summary",
                    "language",
                }.issubset(memory[0])
            )

            claims = _jsonl_rows(result.artifact_paths["news_claims"])
            self.assertEqual(len(claims), len(memory))
            self.assertIn("claim_text", claims[0])
            self.assertIn("confidence", claims[0])
            self.assertIn("source_ids", claims[0])

            outcomes = _jsonl_rows(result.artifact_paths["news_outcomes"])
            statuses = {str(row["status"]) for row in outcomes}
            self.assertIn("confirmed", statuses)
            self.assertIn("unverified", statuses)
            self.assertTrue({"stale", "noise", "duplicate"} & statuses)
            self.assertIn("market_reaction", outcomes[0])
            self.assertIn("market_impact_after_5m", outcomes[0]["market_reaction"])

            attribution = json.loads(result.artifact_paths["news_attribution"].read_text(encoding="utf-8"))
            self.assertIn("source_quality", attribution)
            self.assertIn("topic_quality", attribution)
            self.assertIn("source_diversity", attribution)
            self.assertIn("source_precision", attribution["source_quality"]["cninfo_latest"])
            self.assertIn("unverified_rate", attribution["source_quality"]["eastmoney-724"])
            self.assertIn("topic_signal_quality", attribution["topic_quality"]["order-growth"])

            review_packet = json.loads(
                result.artifact_paths["news_learning_review_packet_json"].read_text(encoding="utf-8")
            )
            self.assertFalse(review_packet["guards"]["auto_code_changes_allowed"])
            self.assertFalse(review_packet["guards"]["auto_live_config_changes_allowed"])
            self.assertFalse(review_packet["guards"]["stock_system_changes_allowed"])
            self.assertFalse(review_packet["guards"]["crypto_system_changes_allowed"])
            self.assertTrue(all("sha256" in item for item in review_packet["artifacts"]))
            self.assertNotIn("news_learning_review_packet_json", {item["name"] for item in review_packet["artifacts"]})
            markdown = result.artifact_paths["news_learning_review_packet_md"].read_text(encoding="utf-8")
            self.assertIn("News Learning Review Packet", markdown)
            self.assertIn("no auto code changes", markdown)
            handoff = result.artifact_paths["news_learning_codex_handoff"].read_text(encoding="utf-8")
            self.assertIn("Codex Handoff", handoff)
            self.assertIn("Do not modify stock system", handoff)

    def test_candidates_are_review_only_and_cli_command_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report_path = root / "latest_report.json"
            output_dir = root / "news_learning"
            self._write_sample_report(report_path)

            parser = build_parser()
            args = parser.parse_args(
                [
                    "news-learning",
                    "--report",
                    str(report_path),
                    "--output-dir",
                    str(output_dir),
                    "--min-source-sample",
                    "1",
                    "--min-topic-sample",
                    "1",
                ]
            )
            self.assertEqual(run_news_learning(args), 0)

            candidates = _jsonl_rows(output_dir / "news_upgrade_candidates.jsonl")
            self.assertGreater(len(candidates), 0)
            for candidate in candidates:
                self.assertEqual(candidate["status"], "research_only")
                self.assertFalse(candidate["allowed_to_auto_apply"])
                self.assertEqual(candidate["promotion_gate"], "manual_codex_review_required")
                self.assertIn("stock system", candidate["blocked_scopes"])
                self.assertIn("crypto system", candidate["blocked_scopes"])

            promotion_report = json.loads((output_dir / "news_promotion_report.json").read_text(encoding="utf-8"))
            self.assertFalse(promotion_report["hard_guards"]["auto_code_changes_allowed"])
            self.assertFalse(promotion_report["hard_guards"]["auto_live_config_changes_allowed"])
            self.assertFalse(promotion_report["hard_guards"]["stock_system_changes_allowed"])
            self.assertFalse(promotion_report["hard_guards"]["crypto_system_changes_allowed"])
            self.assertEqual(promotion_report["hard_guards"]["candidate_status"], "research_review_only")

            export_args = parser.parse_args(
                [
                    "news-learning-export",
                    "--report",
                    str(report_path),
                    "--output-dir",
                    str(output_dir),
                    "--no-copy",
                ]
            )
            self.assertEqual(run_news_learning_export(export_args), 0)
            status_args = parser.parse_args(["news-learning-status", "--output-dir", str(output_dir)])
            self.assertEqual(run_news_learning_status(status_args), 0)

            auto_args = parser.parse_args(
                [
                    "news-learning-auto",
                    "--report",
                    str(report_path),
                    "--output-dir",
                    str(output_dir),
                    "--status-file",
                    str(root / "news_learning_status.json"),
                    "--history-file",
                    str(root / "news_learning_history.jsonl"),
                    "--no-copy",
                ]
            )
            self.assertEqual(run_news_learning_auto(auto_args), 0)
            auto_status = json.loads((root / "news_learning_status.json").read_text(encoding="utf-8"))
            self.assertEqual(auto_status["overall_status"], "ok")
            self.assertEqual(auto_status["modules"][0]["name"], "news_learning")
            self.assertTrue(Path(auto_status["artifacts"]["news_learning_codex_handoff"]).exists())
            self.assertEqual(len((root / "news_learning_history.jsonl").read_text(encoding="utf-8").splitlines()), 1)

    def test_live_automation_attaches_review_packet_paths_without_mutating_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report_path = root / "latest_report.json"
            output_dir = root / "auto_news_learning"
            self._write_sample_report(report_path)
            before = report_path.read_text(encoding="utf-8")
            snapshot = SimpleNamespace(artifacts={})

            _attach_news_learning_artifacts(snapshot, report_path=report_path, output_dir=output_dir)

            self.assertEqual(report_path.read_text(encoding="utf-8"), before)
            self.assertEqual(snapshot.artifacts["news_learning_status"], "ok")
            self.assertGreater(snapshot.artifacts["news_learning_candidate_count"], 0)
            self.assertTrue(Path(snapshot.artifacts["news_learning_review_packet_md"]).exists())
            self.assertTrue(Path(snapshot.artifacts["news_learning_review_packet_json"]).exists())
            self.assertTrue(Path(snapshot.artifacts["news_learning_codex_handoff"]).exists())

            parser = build_parser()
            args = parser.parse_args(["collect"])
            self.assertFalse(args.skip_news_learning)
            self.assertEqual(args.news_learning_dir.name, "news_learning")
            auto_args = parser.parse_args(["news-learning-auto"])
            self.assertEqual(auto_args.status_file.name, "news_learning_status.json")
            self.assertFalse(auto_args.copy_to_clipboard)

    def test_codex_review_command_writes_analysis_without_notifying(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report_path = root / "latest_report.json"
            output_dir = root / "news_learning"
            analysis_path = root / "analysis.md"
            status_path = root / "codex_review_status.json"
            history_path = root / "codex_review_history.jsonl"
            fake_codex = root / "fake-codex"
            self._write_sample_report(report_path)
            fake_codex.write_text(
                "#!/bin/sh\n"
                "out=''\n"
                "while [ $# -gt 0 ]; do\n"
                "  if [ \"$1\" = \"--output-last-message\" ]; then shift; out=\"$1\"; fi\n"
                "  shift\n"
                "done\n"
                "cat >/dev/null\n"
                "printf '新闻学习审阅：建议用户确认是否变更。\\n\\n建议动作：\\n1. candidate-id add_market_impact_label system：值得评估。\\n' > \"$out\"\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)

            parser = build_parser()
            args = parser.parse_args(
                [
                    "news-learning-codex-review",
                    "--report",
                    str(report_path),
                    "--output-dir",
                    str(output_dir),
                    "--analysis-path",
                    str(analysis_path),
                    "--status-file",
                    str(status_path),
                    "--history-file",
                    str(history_path),
                    "--codex-bin",
                    str(fake_codex),
                    "--no-notify",
                    "--timeout",
                    "10",
                ]
            )

            self.assertEqual(run_news_learning_codex_review(args), 0)
            self.assertIn("建议用户确认是否变更", analysis_path.read_text(encoding="utf-8"))
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["overall_status"], "ok")
            self.assertTrue(status["actionable"])
            self.assertFalse(status["notification"]["attempted"])
            self.assertEqual(len(history_path.read_text(encoding="utf-8").splitlines()), 1)

    def _write_sample_report(self, report_path: Path) -> None:
        payload = {
            "created_at": "2026-05-03T10:00:00+00:00",
            "top_events": [
                {
                    "cluster_id": "order-cluster",
                    "headline": "测试公司获得大客户订单公告",
                    "summary": "公告称签订三年订单，金额占上一年收入比例较高。",
                    "direction": "positive",
                    "event_type": "company",
                    "final_score": 88.0,
                    "themes": ["order-growth", "fundamental-signal"],
                    "entities": ["测试公司", "600001"],
                    "source_ids": ["cninfo_latest", "eastmoney-ann"],
                    "doc_count": 2,
                    "top_instruments": [{"symbol": "600001.SH"}],
                    "related_documents": [
                        {
                            "doc_id": "doc-cninfo",
                            "published_at": "2026-05-03T09:55:00+00:00",
                            "source_id": "cninfo_latest",
                            "title": "测试公司获得大客户订单公告",
                            "summary": "公告称签订三年订单，金额占上一年收入比例较高。",
                            "themes": ["order-growth"],
                            "entities": ["测试公司", "600001"],
                            "url": "https://example.com/cninfo-order",
                        },
                        {
                            "doc_id": "doc-eastmoney",
                            "published_at": "2026-05-03T09:56:00+00:00",
                            "source_id": "eastmoney-ann",
                            "title": "测试公司获得大客户订单公告",
                            "summary": "公告称签订三年订单，金额占上一年收入比例较高。",
                            "themes": ["order-growth"],
                            "entities": ["测试公司", "600001"],
                            "url": "https://example.com/eastmoney-order",
                        },
                    ],
                }
            ],
            "positive_catalysts": [],
            "negative_risks": [],
            "watchlist": [],
            "latest_feed": [
                {
                    "doc_id": "doc-weibo-late",
                    "published_at": "2026-05-01T08:00:00+00:00",
                    "source_id": "weibo",
                    "title": "盘前大涨后继续讨论某题材",
                    "summary": "没有明确实体，偏事后价格讨论。",
                    "themes": [],
                    "entities": [],
                    "url": "https://example.com/weibo-late",
                },
                {
                    "doc_id": "doc-xueqiu-noise",
                    "published_at": "2026-05-03T09:58:00+00:00",
                    "source_id": "xueqiu",
                    "title": "今天几个方向还是要重视",
                    "summary": "主观讨论，缺少可验证事实。",
                    "themes": [],
                    "entities": [],
                    "url": "https://example.com/xueqiu-noise",
                },
                {
                    "doc_id": "doc-eastmoney-unverified",
                    "published_at": "2026-05-03T09:59:00+00:00",
                    "source_id": "eastmoney-724",
                    "title": "海外市场早盘出现波动",
                    "summary": "单来源快讯，缺少明确实体、主题和交叉验证。",
                    "themes": [],
                    "entities": [],
                    "url": "https://example.com/eastmoney-unverified",
                },
            ],
            "tech_block": {"signals": []},
        }
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
