from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest

from market_news.application.review_api import LexiconReviewService, ReviewApiStateWriter


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
