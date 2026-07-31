#!/usr/bin/env python3
"""make_demo_news - 造一份演示用的新闻看板，放进 trade/demo_data/news/。

为什么要在本机跑：看板 HTML 由 news collector 的 reporting.py 生成，沙箱里
没有那个包。这里直接调它的 refresh_runtime_status_views()，产出的页面和真实
看板是同一套模板、同一套交互，演示时看到的就是真东西的样子。

内容全部虚构，围绕五只公开的宽基 ETF 编写，不含任何真实新闻、真实机构观点
或用户的持仓信息。
"""
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
NEWS_SRC = HOME / "All here" / "news collector"
DEST = HOME / "All here" / "trade" / "demo_data" / "news" / "reports" / "live"

sys.path.insert(0, str(NEWS_SRC))


def _t(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)) \
        .isoformat(timespec="seconds")


# 虚构事件。刻意用"示例"字样与 DEMO 前缀，避免被误当成真实消息。
EVENTS = [
    dict(cluster_id="demo-001", headline="【示例】宽基 ETF 单日净流入创月内新高",
         summary="示例数据：模型把多只宽基 ETF 的资金流入归为同一事件簇，用于展示聚类与打分。",
         direction="positive", event_type="fund_flow", final_score=0.86,
         heat_score=0.79, importance_score=0.88, confidence_score=0.74,
         markets=["US"]),
    dict(cluster_id="demo-002", headline="【示例】长端美债收益率回落，债券 ETF 走强",
         summary="示例数据：用于展示「利率—债券 ETF」这条传导链在看板上的呈现方式。",
         direction="positive", event_type="macro", final_score=0.81,
         heat_score=0.68, importance_score=0.83, confidence_score=0.77,
         markets=["US"]),
    dict(cluster_id="demo-003", headline="【示例】商品指数回调，能源板块领跌",
         summary="示例数据：用于展示负面事件在看板上的配色与排序。",
         direction="negative", event_type="commodity", final_score=0.78,
         heat_score=0.72, importance_score=0.75, confidence_score=0.69,
         markets=["US"]),
    dict(cluster_id="demo-004", headline="【示例】REITs 板块受租金数据提振",
         summary="示例数据：用于展示单板块事件与标的的关联展示。",
         direction="positive", event_type="sector", final_score=0.72,
         heat_score=0.61, importance_score=0.70, confidence_score=0.71,
         markets=["US"]),
    dict(cluster_id="demo-005", headline="【示例】海外发达市场股指波动率上升",
         summary="示例数据：用于展示风险类事件如何进入观察列表而不是告警。",
         direction="neutral", event_type="volatility", final_score=0.64,
         heat_score=0.58, importance_score=0.62, confidence_score=0.66,
         markets=["US", "EU"]),
]

INSTRUMENTS = [
    dict(cluster_id="demo-001", headline=EVENTS[0]["headline"], symbol="US.SPY",
         market="US", name="SPDR S&P 500 ETF", direction="positive",
         final_score=0.86, reasons=["示例：资金流入", "示例：成交放量"]),
    dict(cluster_id="demo-002", headline=EVENTS[1]["headline"], symbol="US.IEF",
         market="US", name="iShares 7-10Y Treasury", direction="positive",
         final_score=0.81, reasons=["示例：收益率回落"]),
    dict(cluster_id="demo-003", headline=EVENTS[2]["headline"], symbol="US.DBC",
         market="US", name="Invesco DB Commodity", direction="negative",
         final_score=0.78, reasons=["示例：能源权重拖累"]),
    dict(cluster_id="demo-004", headline=EVENTS[3]["headline"], symbol="US.VNQ",
         market="US", name="Vanguard Real Estate ETF", direction="positive",
         final_score=0.72, reasons=["示例：租金数据"]),
    dict(cluster_id="demo-005", headline=EVENTS[4]["headline"], symbol="US.EFA",
         market="US", name="iShares MSCI EAFE ETF", direction="neutral",
         final_score=0.64, reasons=["示例：波动率上行"]),
]

FEED = [
    dict(doc_id=f"demo-doc-{i:03d}", published_at=_t(12 * i),
         source_id="demo_source",
         title=f"【示例新闻 {i}】用于演示采集与去重的占位条目",
         summary="这是演示数据。真实运行时这里是采集器抓到的原文摘要。",
         themes=["示例主题"], entities=["US.SPY"],
         url="https://example.com/demo")
    for i in range(1, 13)
]


def build_report() -> dict:
    alerts = [
        dict(cluster_id=e["cluster_id"], headline=e["headline"],
             level="high" if e["final_score"] > 0.8 else "medium",
             direction=e["direction"], event_type=e["event_type"],
             is_new=(i < 2), final_score=e["final_score"],
             symbols=[INSTRUMENTS[i]["symbol"]],
             reason="示例：分数超过阈值且为新事件" if i < 2 else "示例：分数超过阈值")
        for i, e in enumerate(EVENTS[:3])
    ]
    return {
        "run_id": "demo-run-0001",
        "created_at": _t(3),
        "source": "demo",
        "counts": {"raw_records": 128, "documents": 96, "clusters": 5,
                   "ranked_events": 5, "ranked_instruments": 5},
        "alerts": alerts,
        "alert_counts": {"critical": 0, "high": 2, "medium": 1, "new": 2},
        "top_events": EVENTS,
        "positive_catalysts": [e for e in EVENTS if e["direction"] == "positive"],
        "negative_risks": [e for e in EVENTS if e["direction"] == "negative"],
        "watchlist": [e for e in EVENTS if e["direction"] == "neutral"],
        "top_instruments": INSTRUMENTS,
        "latest_feed": FEED,
        "feature_blocks": {
            "tech_block": {
                "summary": {"signal_count": 3, "tracked_assets": 5,
                            "hot_theme_count": 2, "top_attention_score": 0.86,
                            "lexicon_version": "demo-v1"},
                "lexicon_release": {"version": "demo-v1", "published_at": _t(600),
                                    "reviewer": "demo", "change_note": "演示数据",
                                    "source_trace": {}},
                "signals": [], "themes": [], "asset_ladder": [],
            },
            "lexicon_discovery": {"pending_count": 0, "saved_count": 0,
                                  "relevant_record_count": 0, "candidates": [],
                                  "enabled": False},
        },
        "demo_mode": True,
    }


def main() -> int:
    out = {"kind": "make_demo_news"}
    DEST.mkdir(parents=True, exist_ok=True)
    report_path = DEST / "latest_report.json"
    report_path.write_text(json.dumps(build_report(), ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    out["report"] = str(report_path)

    try:
        from market_news.services.reporting import refresh_runtime_status_views
        refresh_runtime_status_views(report_path)
        html = DEST / "latest_dashboard.html"
        out["html_kb"] = round(html.stat().st_size / 1024) if html.exists() else None
        out["html_ok"] = html.exists()
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    # 生成器会把本机 runtime_status 混进 JSON——那里面有本机路径，删掉
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    removed = payload.pop("runtime_status", None) is not None
    payload["demo_mode"] = True
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    out["stripped_runtime_status"] = removed

    # 看板 HTML 里嵌了一份 runtime_status JSON，里面是各状态文件的绝对路径
    # （/Users/jiao/... 以及 ~/.market_news）。这份 HTML 要进公开仓库，本机用户名
    # 和目录结构不该跟着走，而且演示时那些状态全是 null，显示出来像坏了。
    # 所以逐个替换成占位路径。
    html_file = DEST / "latest_dashboard.html"
    html_text = html_text_before = html_file.read_text(encoding="utf-8", errors="replace")
    for needle, placeholder in (
        (str(HOME / "All here"), "/path/to/workspace"),
        (str(HOME), "/path/to/home"),
        ("jiaojingqi9@gmail.com", "demo@example.com"),
        ("jiaojingqi9", "demo-user"),
    ):
        html_text = html_text.replace(needle, placeholder)
    # 隐藏「Runtime Status」面板。它展示的是采集/推送/健康检查等常驻进程的状态，
    # 演示环境里这些进程根本不存在，画出来是一整片红色「未启动 / 错误」，
    # 第一次看到的人会以为程序坏了。用 CSS 隐藏而不是改生成器：生成器是线上
    # 在用的，不该为了演示去动它。
    hide_css = ("\n<style data-demo=\"1\">/* 演示数据没有常驻进程，"
                "隐藏运行时状态面板 */ .panel.status-panel{display:none !important;}</style>\n")
    if "data-demo=" not in html_text:
        if "</head>" in html_text:
            html_text = html_text.replace("</head>", hide_css + "</head>", 1)
        else:
            html_text = hide_css + html_text

    if html_text != html_text_before:
        html_file.write_text(html_text, encoding="utf-8")
    out["sanitized"] = html_text != html_text_before
    out["hid_status_panel"] = "data-demo=" in html_text

    leaks = [n for n in ("/Users/jiao", "jiaojingqi9") if n in html_text]
    out["path_leaks_after_sanitize"] = leaks
    out["report_kb"] = round(report_path.stat().st_size / 1024, 1)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
