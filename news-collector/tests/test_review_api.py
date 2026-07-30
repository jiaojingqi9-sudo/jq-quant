from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest

from market_news.application.review_api import LexiconReviewService, ReviewApiStateWriter


class FakeAiClient:
    def run_json(self, *, kind: str, instructions: str, payload: dict[str, object]) -> dict[str, object]:
        if kind == "lexicon-review":
            return {
                "reviews": [
                    {
                        "term": payload["candidates"][0]["term"],
                        "action": "add",
                        "term_type": "tech",
                        "confidence": 0.91,
                        "reason": "能稳定识别技术主题。",
                    }
                ]
            }
        return {
            "worth_attention": True,
            "attention_score": 82,
            "summary": "订单变化会影响收入预期。",
            "impact_logic": ["客户订单变化会改变收入和估值假设。"],
            "affected_assets": [
                {
                    "symbol": "300000.SZ",
                    "name": "测试公司",
                    "market": "CN-A",
                    "direction": "negative",
                    "reason": "直接客户订单变化。",
                }
            ],
            "watch_points": ["后续公告确认订单金额。"],
            "missing_evidence": ["订单占收入比例。"],
            "conclusion": "值得继续跟踪。",
            "confidence": 0.86,
        }


class ReviewApiTest(unittest.TestCase):
    def _write_json(self, path: Path, payload: object) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_add_term_moves_candidate_into_lexicon(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lexicon_path = root / "tech_lexicon.json"
            discovery_path = root / "lexicon_discovery.jsonl"
            report_path = root / "latest_report.json"
            status_path = root / "review_api_status.json"
            history_path = root / "review_api_history.jsonl"

            self._write_json(
                lexicon_path,
                [
                    {
                        "canonical_text": "AI算力",
                        "term_type": "theme",
                        "synonyms": ["AI算力"],
                        "impact_vector": {"ai-compute": 0.9},
                    }
                ],
            )
            report_path.write_text("{}", encoding="utf-8")
            discovery_path.write_text(
                json.dumps(
                    {
                        "text": "OpenClaw",
                        "raw_freq": 5,
                        "cooccurrence": {"AI算力": 3.0},
                        "inferred_impact": {"ai-compute": 0.42},
                        "discovery_score": 12.5,
                        "example_snippets": ["OpenClaw 是新的 agent 框架"],
                        "detected_at": "2026-03-16T00:00:00+00:00",
                        "status": "pending",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            service = LexiconReviewService(
                lexicon_path=lexicon_path,
                discovery_path=discovery_path,
                report_path=report_path,
                tech_block_config={"unknown_term_detector": {}},
                status_writer=ReviewApiStateWriter(status_path=status_path, history_path=history_path),
            )

            payload = service.add_term("OpenClaw", term_type="tech")

            self.assertEqual(payload["summary"]["pending_count"], 0)
            self.assertEqual(payload["summary"]["accepted_count"], 2)
            self.assertEqual(len(payload["accepted_terms"]), 2)
            lexicon = json.loads(lexicon_path.read_text(encoding="utf-8"))
            added = next(item for item in lexicon if item["canonical_text"] == "OpenClaw")
            self.assertEqual(added["term_type"], "tech")
            rows = [json.loads(line) for line in discovery_path.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(rows[0]["status"], "accepted")
            self.assertTrue(status_path.exists())

    def test_reject_term_updates_discovery_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lexicon_path = root / "tech_lexicon.json"
            discovery_path = root / "lexicon_discovery.jsonl"
            report_path = root / "latest_report.json"

            self._write_json(lexicon_path, [])
            report_path.write_text("{}", encoding="utf-8")
            discovery_path.write_text(
                json.dumps(
                    {
                        "text": "基础设施",
                        "raw_freq": 4,
                        "cooccurrence": {"服务器": 2.0},
                        "inferred_impact": {"server-chain": 0.33},
                        "discovery_score": 9.1,
                        "example_snippets": ["基础设施成为算力扩张的关键配套"],
                        "detected_at": "2026-03-16T00:00:00+00:00",
                        "status": "pending",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            service = LexiconReviewService(
                lexicon_path=lexicon_path,
                discovery_path=discovery_path,
                report_path=report_path,
                tech_block_config={"unknown_term_detector": {}},
            )

            payload = service.reject_term("基础设施")

            self.assertEqual(payload["summary"]["pending_count"], 0)
            rows = [json.loads(line) for line in discovery_path.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(rows[0]["status"], "rejected")

    def test_ai_review_pending_terms_auto_applies_recommendation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lexicon_path = root / "tech_lexicon.json"
            discovery_path = root / "lexicon_discovery.jsonl"
            report_path = root / "latest_report.json"

            self._write_json(lexicon_path, [])
            report_path.write_text("{}", encoding="utf-8")
            discovery_path.write_text(
                json.dumps(
                    {
                        "text": "先进封装",
                        "raw_freq": 5,
                        "cooccurrence": {"半导体": 3.0},
                        "inferred_impact": {"semiconductor": 0.65},
                        "discovery_score": 15.0,
                        "example_snippets": ["先进封装需求提升。"],
                        "detected_at": "2026-03-16T00:00:00+00:00",
                        "status": "pending",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            service = LexiconReviewService(
                lexicon_path=lexicon_path,
                discovery_path=discovery_path,
                report_path=report_path,
                tech_block_config={"unknown_term_detector": {}},
                ai_client=FakeAiClient(),
            )

            payload = service.ai_review_pending_terms(limit=10)

            self.assertEqual(payload["ai_review"][0]["action"], "add")
            self.assertEqual(payload["summary"]["pending_count"], 0)
            self.assertEqual(payload["summary"]["accepted_count"], 1)
            self.assertEqual(payload["auto_review"]["accepted"], 1)
            self.assertIn("AI自动审核完成", payload["message"])
            lexicon = json.loads(lexicon_path.read_text(encoding="utf-8"))
            self.assertEqual(lexicon[0]["canonical_text"], "先进封装")
            self.assertEqual(lexicon[0]["term_type"], "tech")
            rows = [json.loads(line) for line in discovery_path.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(rows[0]["status"], "accepted")
            self.assertEqual(rows[0]["ai_review_action"], "add")

    def test_ai_review_low_confidence_add_is_rejected(self) -> None:
        class LowConfidenceAiClient:
            def run_json(self, *, kind: str, instructions: str, payload: dict[str, object]) -> dict[str, object]:
                return {
                    "reviews": [
                        {
                            "term": payload["candidates"][0]["term"],
                            "action": "add",
                            "term_type": "theme",
                            "confidence": 0.2,
                            "reason": "证据太弱。",
                        }
                    ]
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lexicon_path = root / "tech_lexicon.json"
            discovery_path = root / "lexicon_discovery.jsonl"
            report_path = root / "latest_report.json"

            self._write_json(lexicon_path, [])
            report_path.write_text("{}", encoding="utf-8")
            discovery_path.write_text(
                json.dumps(
                    {
                        "text": "具身智能",
                        "raw_freq": 10,
                        "cooccurrence": {"机器人": 2.0},
                        "inferred_impact": {"robotics": 0.42},
                        "discovery_score": 20.0,
                        "example_snippets": ["具身智能带动机器人产业链关注。"],
                        "detected_at": "2026-03-16T00:00:00+00:00",
                        "status": "pending",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            service = LexiconReviewService(
                lexicon_path=lexicon_path,
                discovery_path=discovery_path,
                report_path=report_path,
                tech_block_config={"unknown_term_detector": {}},
                ai_client=LowConfidenceAiClient(),
            )

            payload = service.ai_review_pending_terms(limit=10)

            self.assertEqual(payload["auto_review"]["accepted"], 0)
            self.assertEqual(payload["auto_review"]["rejected"], 1)
            self.assertEqual(json.loads(lexicon_path.read_text(encoding="utf-8")), [])
            rows = [json.loads(line) for line in discovery_path.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(rows[0]["status"], "rejected")
            self.assertIn("低于", rows[0]["ai_review_reason"])

    def test_pending_payload_force_rejects_generic_price_terms(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lexicon_path = root / "tech_lexicon.json"
            discovery_path = root / "lexicon_discovery.jsonl"
            report_path = root / "latest_report.json"

            self._write_json(lexicon_path, [])
            report_path.write_text("{}", encoding="utf-8")
            discovery_path.write_text(
                json.dumps(
                    {
                        "text": "跌幅",
                        "raw_freq": 9,
                        "cooccurrence": {"半导体": 2.0},
                        "inferred_impact": {"semiconductor": 0.7},
                        "discovery_score": 19.0,
                        "example_snippets": ["跌幅较大。"],
                        "detected_at": "2026-03-16T00:00:00+00:00",
                        "status": "pending",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            service = LexiconReviewService(
                lexicon_path=lexicon_path,
                discovery_path=discovery_path,
                report_path=report_path,
                tech_block_config={"unknown_term_detector": {}},
            )

            payload = service.pending_payload()

            self.assertEqual(payload["summary"]["pending_count"], 0)
            rows = [json.loads(line) for line in discovery_path.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(rows[0]["status"], "rejected")
            self.assertIn("硬规则拒绝", rows[0]["ai_review_reason"])

    def test_manual_news_analysis_uses_ai_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lexicon_path = root / "tech_lexicon.json"
            discovery_path = root / "lexicon_discovery.jsonl"
            report_path = root / "latest_report.json"

            self._write_json(lexicon_path, [])
            discovery_path.write_text("", encoding="utf-8")
            report_path.write_text("{}", encoding="utf-8")

            service = LexiconReviewService(
                lexicon_path=lexicon_path,
                discovery_path=discovery_path,
                report_path=report_path,
                tech_block_config={"unknown_term_detector": {}},
                ai_client=FakeAiClient(),
            )

            payload = service.analyze_news_text(
                text="公司披露主要客户取消订单，预计影响全年收入。",
                question="是否值得看？",
            )

            self.assertTrue(payload["analysis"]["worth_attention"])
            self.assertEqual(payload["analysis"]["affected_assets"][0]["symbol"], "300000.SZ")

    def test_remove_term_deletes_from_lexicon_and_rejects_discovery_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lexicon_path = root / "tech_lexicon.json"
            discovery_path = root / "lexicon_discovery.jsonl"
            report_path = root / "latest_report.json"

            self._write_json(
                lexicon_path,
                [
                    {
                        "canonical_text": "OpenClaw",
                        "term_type": "tech",
                        "synonyms": ["OpenClaw", "openclaw"],
                        "impact_vector": {"ai-compute": 0.6},
                    }
                ],
            )
            report_path.write_text("{}", encoding="utf-8")
            discovery_path.write_text(
                json.dumps(
                    {
                        "text": "OpenClaw",
                        "raw_freq": 5,
                        "cooccurrence": {"AI算力": 3.0},
                        "inferred_impact": {"ai-compute": 0.42},
                        "discovery_score": 12.5,
                        "example_snippets": ["OpenClaw 是新的 agent 框架"],
                        "detected_at": "2026-03-16T00:00:00+00:00",
                        "status": "accepted",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            service = LexiconReviewService(
                lexicon_path=lexicon_path,
                discovery_path=discovery_path,
                report_path=report_path,
                tech_block_config={"unknown_term_detector": {}},
            )

            payload = service.remove_term("OpenClaw")

            self.assertEqual(payload["summary"]["pending_count"], 0)
            self.assertEqual(payload["summary"]["accepted_count"], 0)
            self.assertEqual(payload["accepted_terms"], [])
            lexicon = json.loads(lexicon_path.read_text(encoding="utf-8"))
            self.assertEqual(lexicon, [])
            rows = [json.loads(line) for line in discovery_path.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(rows[0]["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
