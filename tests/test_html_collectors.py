from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import unittest

from market_news.infrastructure.collectors.cls import ClsTelegraphCollector
from market_news.infrastructure.collectors.eastmoney import EastmoneyCollector
from market_news.infrastructure.collectors.gelonghui import GelonghuiLiveCollector
from market_news.infrastructure.collectors.html_source import HtmlListDetailCollector, HtmlSourceSpec
from market_news.infrastructure.collectors.factory import build_live_collector
from market_news.infrastructure.collectors.rss import FeedSpec, RSSCollector


@dataclass
class _FakeResponse:
    url: str
    text: str


class _FakeHttpClient:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    def get_text(self, url: str, **_: object) -> _FakeResponse:
        if url not in self.pages:
            raise FileNotFoundError(url)
        return _FakeResponse(url=url, text=self.pages[url])


class HtmlCollectorTest(unittest.TestCase):
    def test_list_detail_collector_extracts_candidates_and_detail_text(self) -> None:
        pages = {
            "https://example.com/list": """
                <html><body>
                  <a href="/news/1.html">AI芯片量产带动服务器升级</a>
                  <a href="/news/ignore.html">首页</a>
                </body></html>
            """,
            "https://example.com/news/1.html": """
                <html><body>
                  <div class="article">
                    <p>2026-03-15 10:30:00</p>
                    <p>AI芯片量产进入新阶段，服务器链和光模块链受到关注。</p>
                    <p>相关公司开始扩产并加快客户导入。</p>
                  </div>
                </body></html>
            """,
        }
        collector = HtmlListDetailCollector(
            _FakeHttpClient(pages),
            HtmlSourceSpec(
                source_id="demo_html",
                name="demo-html",
                url="https://example.com/list",
                source_trust=0.9,
                include_link_patterns=["/news/"],
                include_title_patterns=["AI|芯片|服务器"],
                body_container_patterns=["<div[^>]+class=\\\"article\\\"[^>]*>.*?</div>"],
            ),
        )

        records = collector.collect()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].title, "AI芯片量产带动服务器升级")
        self.assertIn("服务器链", records[0].body)
        self.assertIn("AI芯片量产进入新阶段", records[0].summary)
        self.assertEqual(records[0].published_at.isoformat(), "2026-03-15T10:30:00+00:00")

    def test_factory_skips_disabled_sources(self) -> None:
        root = Path(__file__).resolve().parent.parent
        config_path = root / "config" / "live_sources.json"

        collector = build_live_collector(config_path, user_agent="test-agent")

        names = [item.name for item in collector.collectors]
        self.assertIn("weibo", names)
        self.assertIn("xueqiu", names)
        self.assertIn("cls", names)
        self.assertIn("csrc-home-updates", names)
        self.assertIn("eastmoney", names)
        self.assertIn("cninfo-latest-announcements", names)
        self.assertIn("xinhua-tech-home", names)
        self.assertNotIn("tmtpost", names)
        self.assertNotIn("gelonghui-hk", names)
        self.assertNotIn("huxiu-tech-channel", names)
        self.assertNotIn("ifeng-tech-home", names)

    def test_cls_collector_falls_back_to_html_page(self) -> None:
        pages = {
            "https://www.cls.cn/nodeapi/updateTelegraph?app=CLS&os=web&sv=7.7.5&rn=2&last_time=0": "<html>not-json</html>",
            "https://www.cls.cn/telegraph": """
                <html><body>
                  <div class="p-t-20 p-b-20 b-b-w-1 b-b-s-s b-c-e6e7ea">
                    <div>
                      <div class="clearfix m-b-15 f-s-16 telegraph-content-box">
                        <div class="clearfix p-r l-h-26p ">
                          <span class="f-l l-h-136363 f-w-b c-de0422 telegraph-time-box">06:25:10</span>
                          <span class="c-34304b"><div><strong>【核电订单增长】</strong>财联社3月16日电，核电设备订单增速明显提升。<br /></div></span>
                        </div>
                      </div>
                      <div class="clearfix">
                        <a class="f-s-12 bg-c-f1f1f1 b-c-e6e7ea label-item" href="/subject/1">核电</a>
                      </div>
                      <div class="clearfix f-s-12 c-222 subject-bottom-box">
                        <div class="clearfix f-r">
                          <div class="f-l m-r-15"><a href="/detail/2313490" class="f-s-12 c-222">评论</a></div>
                        </div>
                      </div>
                    </div>
                  </div></div></div>
                </body></html>
            """,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            collector = ClsTelegraphCollector(
                http_client=_FakeHttpClient(pages),
                page_size=2,
                last_time_file=Path(temp_dir) / "cls_last_time.txt",
            )

            records = collector.collect()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].source_id, "cls")
        self.assertEqual(records[0].external_id, "2313490")
        self.assertIn("核电设备订单增速明显提升", records[0].body)
        self.assertIn("核电", records[0].metadata["subjects"])

    def test_gelonghui_collector_extracts_ssr_live_items(self) -> None:
        pages = {
            "https://www.gelonghui.com/live": """
                <html><body><script>
                __NUXT__=(function(a,b,c,d,e){return {layout:"default",data:[{data:[{id:2348179,title:"港股IPO动态：今日飞速创新申购",createTimestamp:1742083200000,updateTimestamp:1742083200000,count:{read:1},content:"格隆汇3月16日｜今日飞速创新(3355.HK)申购，无新股上市。",contentPrefix:d,relatedStocks:[{market:"hk",code:"03355",name:"飞速创新",canClick:true}],route:"https:\\u002F\\u002Fwww.gelonghui.com\\u002Flive\\u002F2348179",closeComment:c}]}]}})(null,0,false,\"\",1);
                </script></body></html>
            """
        }
        collector = GelonghuiLiveCollector(_FakeHttpClient(pages), item_limit=5)

        records = collector.collect()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].source_id, "gelonghui")
        self.assertEqual(records[0].external_id, "2348179")
        self.assertIn("飞速创新", records[0].body)
        self.assertEqual(records[0].metadata["direct_codes"], ["03355"])

    def test_eastmoney_collector_maps_announcements_news_and_focus(self) -> None:
        pages = {
            "https://np-anotice-stock.eastmoney.com/api/security/ann?page_index=1&page_size=50&ann_type=A&client_source=web": json.dumps(
                {
                    "data": {
                        "list": [
                            {
                                "art_code": "AN123",
                                "title": "特锐德:关于中标青海油田风电项目的提示性公告",
                                "notice_date": "2026-03-15 10:30:00",
                                "codes": [
                                    {
                                        "stock_code": "300001",
                                        "short_name": "特锐德",
                                        "ann_type": "A"
                                    }
                                ],
                                "columns": [{"column_name": "重大合同"}]
                            }
                        ]
                    }
                }
            ),
            "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html": 'var ajaxResult={"LivesList":[{"id":"N1","newsid":"N1","title":"AI服务器景气度持续上行","digest":"多家厂商扩产，服务器链关注度提升。","url_w":"https://finance.eastmoney.com/a/202603150001.html","showtime":"2026-03-15 11:00:00","column":"100,102","newstype":"1"}]};',
            "https://newsapi.eastmoney.com/kuaixun/v1/getlist_350_ajaxResult_50_1_.html": '{"LivesList":[{"id":"F1","newsid":"F1","title":"焦点板块：国产光刻胶突破带动半导体材料链活跃","digest":"ArF光刻胶量产预期升温。","url_w":"https://finance.eastmoney.com/a/202603150002.html","showtime":"2026-03-15 11:05:00","column":"350","newstype":"1"}]}var ajaxResult='
        }

        collector = EastmoneyCollector(
            _FakeHttpClient(pages),
            endpoints=["ann-a", "news", "focus"],
            max_records_per_endpoint=5,
        )

        records = collector.collect()

        self.assertEqual(len(records), 3)
        ann_record = next(item for item in records if item.source_id == "eastmoney-ann")
        news_record = next(item for item in records if item.source_id == "eastmoney-724")
        focus_record = next(item for item in records if item.source_id == "eastmoney-focus")
        self.assertEqual(ann_record.external_id, "AN123")
        self.assertEqual(ann_record.metadata["stock_code"], "300001")
        self.assertIn("重大合同", ann_record.metadata["column_names"])
        self.assertEqual(news_record.external_id, "N1")
        self.assertIn("服务器链", news_record.summary)
        self.assertEqual(news_record.metadata["endpoint"], "news")
        self.assertEqual(focus_record.external_id, "F1")
        self.assertEqual(focus_record.metadata["endpoint"], "focus")
        self.assertAlmostEqual(focus_record.source_trust, 0.88, places=2)

    def test_rss_collector_strips_html_from_summary(self) -> None:
        pages = {
            "https://example.com/feed.xml": """
                <rss version="2.0">
                  <channel>
                    <item>
                      <title><![CDATA[AI 电源赛道融资进展]]></title>
                      <link>https://example.com/p/1</link>
                      <guid>1</guid>
                      <pubDate>Sun, 16 Mar 2026 00:00:00 GMT</pubDate>
                      <description><![CDATA[
                        <p>作者 | 张三</p>
                        <p>公司完成新一轮融资，并加快 AIDC 电源产品交付。</p>
                        <p><img src="https://example.com/image.jpg" /></p>
                      ]]></description>
                    </item>
                  </channel>
                </rss>
            """
        }

        collector = RSSCollector(
            _FakeHttpClient(pages),
            FeedSpec(
                source_id="demo-rss",
                name="demo-rss",
                url="https://example.com/feed.xml",
                source_trust=0.8,
            ),
        )

        records = collector.collect()

        self.assertEqual(len(records), 1)
        self.assertNotIn("<p>", records[0].summary)
        self.assertNotIn("img src", records[0].summary)
        self.assertIn("公司完成新一轮融资", records[0].summary)


if __name__ == "__main__":
    unittest.main()
