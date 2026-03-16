from __future__ import annotations

from pathlib import Path
import unittest

from market_news.infrastructure.collectors.xueqiu import XueqiuCollector
from market_news.infrastructure.http import UrllibHttpClient


class XueqiuCollectorTest(unittest.TestCase):
    def test_homepage_parser_extracts_timeline_cards(self) -> None:
        collector = XueqiuCollector(
            queries=["AI概念", "算力"],
            cookie_path=Path("/tmp/xueqiu_cookies.json"),
            http_client=UrllibHttpClient(user_agent="test-agent", timeout=8),
            max_results_per_query=5,
        )
        html = """
        <html><body>
          <article class="style_timeline__item_3WW">
            <a data-screenname="门捷列夫学徒"></a>
            <a class="style_date-and-source_3r-" href="/1281413694/379501999">
              修改于8小时前&nbsp;·&nbsp;<span>来自iPhone</span>
            </a>
            <h3 class="style_timeline__item__title_3bq">AI 电源进入新周期</h3>
            <div class="style_content_1G- style_content--description_1KV">
              AIDC 电源和算力基建继续升温，服务器产业链受关注。<br>相关公司开始扩产。
            </div>
          </article>
        </body></html>
        """

        records = collector._records_from_homepage_html(html, limit=5)
        filtered = collector._filter_records(records)

        self.assertEqual(len(records), 1)
        self.assertEqual(len(filtered), 1)
        record = filtered[0]
        self.assertEqual(record.source_id, "xueqiu")
        self.assertEqual(record.external_id, "379501999")
        self.assertEqual(record.metadata["author"], "门捷列夫学徒")
        self.assertEqual(record.metadata["origin"], "来自iPhone")
        self.assertIn("算力", record.metadata["query_hits"])
        self.assertIn("AIDC 电源", record.summary)


if __name__ == "__main__":
    unittest.main()
