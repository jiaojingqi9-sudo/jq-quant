from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest

from market_news.infrastructure.cookie_store import install_cookie_file, load_cookies, market_news_cookie_dir
from market_news.services.lexicon_feedback import LexiconFeedbackStore
from market_news.services.lexicon_suggester import LexiconSuggester


class LexiconToolsTest(unittest.TestCase):
    def test_feedback_store_aggregates_votes_and_suggester_outputs_confidence_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            feedback_path = temp_path / "lexicon_feedback.jsonl"
            store = LexiconFeedbackStore(feedback_path)
            for _ in range(5):
                store.record(signal_id="c1", result="good", matched_terms=["光模块"], note="")
            for _ in range(1):
                store.record(signal_id="c2", result="bad", matched_terms=["光模块"], note="")

            feedback = store.aggregate()
            suggestions = LexiconSuggester().suggest(
                feedback=feedback,
                current_lexicon=[
                    {
                        "canonical_text": "光模块",
                        "synonyms": ["光模块", "800g"],
                        "base_confidence": 0.70,
                    }
                ],
                min_feedback_count=5,
            )

            self.assertEqual(feedback["光模块"]["good"], 5)
            self.assertEqual(feedback["光模块"]["bad"], 1)
            self.assertEqual(len(suggestions), 1)
            self.assertGreater(suggestions[0]["suggested_base_confidence"], 0.70)

    def test_cookie_store_installs_and_loads_cookie_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source = temp_path / "cookies.json"
            source.write_text(json.dumps({"SUB": "abc", "SUBP": "def"}), encoding="utf-8")
            installed = install_cookie_file(source, target_name="test_cookie.json")
            try:
                self.assertTrue(installed.exists())
                self.assertEqual(load_cookies(installed)["SUB"], "abc")
                self.assertEqual(installed.parent, market_news_cookie_dir())
            finally:
                installed.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
