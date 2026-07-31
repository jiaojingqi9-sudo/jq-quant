from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import unittest

from market_news.infrastructure.collectors.cls import ClsTelegraphCollector
from market_news.infrastructure.collectors.eastmoney import EastmoneyCollector
from market_news.infrastructure.collectors.eastmoney_topic import (
    EastmoneyTopicCollector,
    EastmoneyTopicSpec,
)
from market_news.infrastructure.collectors.gelonghui import GelonghuiLiveCollector
from market_news.infrastructure.collectors.html_source import HtmlListDetailCollector, HtmlSourceSpec
from market_news.infrastructure.collectors.factory import build_live_collector
from market_news.infrastructure.collectors.rss import FeedSpec, RSSCollector
from market_news.infrastructure.collectors.sfc_offer_periods import (
    SFCOfferPeriodsCollector,
    SFCOfferPeriodsSpec,
)


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

    def post_text(self, url: str, **_: object) -> _FakeResponse:
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

    def test_list_detail_collector_prefers_anchor_title_attribute(self) -> None:
        pages = {
            "https://example.com/list": """
                <html><body>
                  <a href="/news/space.html" title="国家航天局召开商业航天高质量发展企业圆桌会议">
                    <div>国家航天局召开商业航天高质量发展企业圆桌会议</div>
                    <div>这是一段很长很长的摘要，用来模拟政府网站把摘要也包进链接里的结构。
                    系统应该优先使用 title 属性，否则内部文本可能超过标题长度限制而被过滤。</div>
                  </a>
                </body></html>
            """,
            "https://example.com/news/space.html": """
                <html><body>
                  <div class="article">
                    <p>商业航天许可准入、发射申请和频率协调进入一站式审批。</p>
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
                include_title_patterns=["商业航天"],
                body_container_patterns=["<div[^>]+class=\\\"article\\\"[^>]*>.*?</div>"],
            ),
        )

        records = collector.collect()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].title, "国家航天局召开商业航天高质量发展企业圆桌会议")

    def test_factory_skips_disabled_sources(self) -> None:
        root = Path(__file__).resolve().parent.parent
        config_path = root / "config" / "live_sources.json"

        collector = build_live_collector(config_path, user_agent="test-agent")

        names = [item.name for item in collector.collectors]

        # 只断言机制，不断言某个源当前开着还是关着。
        #
        # 这里原来写 assertIn("weibo", names)，于是 2026-07-31 把微博关掉
        # （信噪比太低）之后测试立刻红了——它测的其实是「当前配置恰好是什么」，
        # 而不是「被禁用的源会不会被跳过」。真正该守住的是后者：只要
        # live_sources.json 里 enabled=false，工厂就不能把它建出来。
        # 这个 bug 有前科：RSS 源的 enabled 开关曾长期没被读取，直到
        # 2026-07-30 才发现，正是因为没有测试盯住这条规则。
        with open(config_path, encoding="utf-8") as handle:
            live_cfg = json.load(handle)
        for key in ("weibo", "xueqiu"):
            cfg = live_cfg.get(key)
            if not isinstance(cfg, dict):
                continue
            if cfg.get("enabled", False):
                self.assertIn(key, names, f"{key} 配置为启用，却没被构造出来")
            else:
                self.assertNotIn(key, names, f"{key} 配置为禁用，却仍被构造出来")

        self.assertIn("cls", names)
        self.assertIn("csrc-home-updates", names)
        self.assertIn("eastmoney", names)
        self.assertIn("eastmoney-topic", names)
        self.assertIn("cninfo-latest-announcements", names)
        self.assertIn("xinhua-tech-home", names)
        self.assertIn("sfc-offer-periods", names)
        self.assertIn("cnsa-news", names)
        self.assertIn("spacechina-news", names)
        self.assertIn("spacechina-innovation", names)
        self.assertIn("pbc-news", names)
        self.assertIn("nfra-news", names)
        self.assertIn("nhsa-news", names)
        self.assertIn("mofcom-news", names)
        self.assertIn("mot-news", names)
        self.assertIn("nda-news", names)
        # gacc-news / mohurd-news / sasac-news are intentionally left out: those
        # sites are unreachable from outside mainland China (verified 2026-07-30
        # from two independent networks) and are disabled in live_sources.json.
        # Asserting on a specific source's enabled state makes this test fail
        # every time a source is toggled, so only the mechanism is asserted below.
        self.assertIn("sse-announcements", names)
        self.assertIn("szse-announcements", names)
        self.assertNotIn("tmtpost", names)
        self.assertNotIn("gelonghui-hk", names)
        self.assertNotIn("huxiu-tech-channel", names)
        self.assertNotIn("ifeng-tech-home", names)

    def test_eastmoney_topic_collector_extracts_homepage_and_history_topics(self) -> None:
        pages = {
            "https://gubatopic.eastmoney.com/interface/GetData.aspx?path=newtopic/api/Topic/HomePageListRead": json.dumps(
                {
                    "rc": 1,
                    "count": 2,
                    "re": [
                        {
                            "htid": 11554,
                            "nickname": "阿里云上调AI算力价格，需求持续旺盛",
                            "introduction": "算力租赁概念股午后持续拉升",
                            "desc": "阿里云官网发布AI算力、存储等产品调价公告。",
                            "clickNumber": 302977,
                            "collectNumber": 0,
                            "postNumber": 418,
                            "isRecommend": False,
                            "stock_list": [
                                {"code": "bk1134", "name": "算力概念", "market": "-1", "qmarket": 90, "qcode": "BK1134"},
                                {"code": "603300", "name": "海南华铁", "market": "100", "qmarket": 1, "qcode": "603300"},
                            ],
                        }
                    ],
                }
            ),
            "https://gubatopic.eastmoney.com/interface/GetData.aspx?path=newtopic/api/Topic/HistoryTopicRead": json.dumps(
                {
                    "rc": 1,
                    "count": 1,
                    "re": [
                        {
                            "htime": "2026-03-17T00:00:00",
                            "historyTopic": [
                                {
                                    "htid": 11638,
                                    "name": "蚂蚁集团要约收购耀才证券已获批准",
                                    "stock_list": [
                                        {"code": "hk01428", "name": "耀才证券金融", "market": "106", "qmarket": 116, "qcode": "01428"},
                                        {"code": "hk06099", "name": "招商证券", "market": "106", "qmarket": 116, "qcode": "06099"},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ),
        }
        collector = EastmoneyTopicCollector(
            _FakeHttpClient(pages),
            EastmoneyTopicSpec(homepage_limit=5, history_limit=5),
        )

        records = collector.collect()

        self.assertEqual(len(records), 2)
        self.assertTrue(all(record.source_id == "eastmoney-topic" for record in records))
        self.assertEqual(records[0].external_id, "11554")
        self.assertIn("AI算力价格", records[0].title)
        self.assertIn("海南华铁", records[0].entities)
        self.assertEqual(records[1].external_id, "11638")
        self.assertIn("耀才证券金融", records[1].entities)
        self.assertIn("HK", records[1].regions)

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

    def test_sfc_offer_periods_collector_extracts_current_offer_rows(self) -> None:
        pages = {
            "https://www.sfc.hk/en/Regulatory-functions/Corporates/Takeovers-and-mergers/offer-periods": """
                <html><body>
                  <div class="table-container main-style offer-periods-table" data-target="0">
                    <table>
                      <tbody>
                        <tr>
                          <th>Offeree Company</th>
                          <th>Stock Code</th>
                          <th>Offeror</th>
                          <th>Relevant securities</th>
                          <th>Date of commencement of offer period</th>
                          <th>Date of publication of announcement</th>
                        </tr>
                      </tbody>
                      <tbody class="append-here">
                        <tr>
                          <td>Bright Smart Securities &amp; Commodities Group Limited</td>
                          <td>01428</td>
                          <td>Wealthiness and Prosperity Holding Limited</td>
                          <td>Bright Smart Securities &amp; Commodities Group Limited</td>
                          <td data-date="25 Apr 2025">25 Apr 2025</td>
                          <td data-date="25 Apr 2025">25 Apr 2025</td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                </body></html>
            """,
        }

        collector = SFCOfferPeriodsCollector(
            _FakeHttpClient(pages),
            SFCOfferPeriodsSpec(item_limit=3),
        )

        records = collector.collect()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].source_id, "sfc-offer-periods")
        self.assertIn("Bright Smart Securities", records[0].title)
        self.assertIn("Wealthiness and Prosperity", records[0].body)
        self.assertEqual(records[0].metadata["stock_code"], "01428")
        self.assertEqual(records[0].metadata["instrument_market"], "HK")

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
