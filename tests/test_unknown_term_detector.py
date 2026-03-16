from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from market_news.domain.models import RawNewsRecord
from market_news.services.unknown_term_detector import UnknownTermDetector


class UnknownTermDetectorTest(unittest.TestCase):
    def test_detector_discovers_unknown_terms_and_persists_status(self) -> None:
        lexicon = [
            {
                "canonical_text": "机器人",
                "synonyms": ["机器人", "人形机器人"],
                "impact_vector": {"robotics": 1.0},
            },
            {
                "canonical_text": "航空航天",
                "synonyms": ["航空航天", "航天"],
                "impact_vector": {"aerospace-defense": 1.0},
            },
        ]
        detector = UnknownTermDetector(
            lexicon=lexicon,
            config={"min_freq": 2, "min_discovery_score": 2.0, "max_candidates_per_run": 10},
        )
        records = [
            RawNewsRecord(
                source_id="demo",
                title="碳纤维进入机器人骨架",
                summary="国产碳纤维在人形机器人骨架中放量。",
                body="碳纤维与机器人产业链协同加深。",
            ),
            RawNewsRecord(
                source_id="demo",
                title="航空航天也在使用碳纤维",
                summary="碳纤维用于航空航天复材结构。",
                body="碳纤维和航空航天需求持续增长。",
            ),
            RawNewsRecord(
                source_id="demo",
                title="又一篇提到碳纤维",
                summary="碳纤维仍是高景气材料。",
                body="机器人厂商开始验证碳纤维部件。",
            ),
        ]

        candidates = detector.run(records)
        self.assertTrue(any(item.text == "碳纤维" for item in candidates))
        candidate = next(item for item in candidates if item.text == "碳纤维")
        self.assertGreaterEqual(candidate.raw_freq, 3)
        self.assertIn("robotics", candidate.inferred_impact)

        with tempfile.TemporaryDirectory() as temp_dir:
            discovery_path = Path(temp_dir) / "lexicon_discovery.jsonl"
            detector.save(candidates, discovery_path)
            pending = detector.list_pending(discovery_path, min_score=2.0, limit=10)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["text"], "碳纤维")
            self.assertTrue(detector.set_status(discovery_path, "碳纤维", "accepted"))
            rows = [json.loads(line) for line in discovery_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(rows[0]["status"], "accepted")

    def test_list_pending_hides_generic_noise_rows(self) -> None:
        lexicon = [
            {
                "canonical_text": "机器人",
                "synonyms": ["机器人", "人形机器人"],
                "impact_vector": {"robotics": 1.0},
            }
        ]
        detector = UnknownTermDetector(
            lexicon=lexicon,
            config={"min_freq": 2, "min_discovery_score": 2.0, "min_top_impact_score": 0.35},
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            discovery_path = Path(temp_dir) / "lexicon_discovery.jsonl"
            discovery_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "text": "全文",
                                "raw_freq": 9,
                                "cooccurrence": {"机器人": 3},
                                "inferred_impact": {"robotics": 0.21, "ai-hardware": 0.19},
                                "discovery_score": 5.0,
                                "example_snippets": ["全文如下"],
                                "detected_at": "2026-03-15T00:00:00Z",
                                "status": "pending",
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "text": "碳纤维",
                                "raw_freq": 6,
                                "cooccurrence": {"机器人": 4},
                                "inferred_impact": {"robotics": 0.62, "new-materials": 0.55},
                                "discovery_score": 4.8,
                                "example_snippets": ["碳纤维机器人骨架"],
                                "detected_at": "2026-03-15T00:01:00Z",
                                "status": "pending",
                            },
                            ensure_ascii=False,
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            pending = detector.list_pending(discovery_path, min_score=2.0, limit=10)
            self.assertEqual([row["text"] for row in pending], ["碳纤维"])


if __name__ == "__main__":
    unittest.main()
