"""市场新闻功能。

这个功能依赖一个可选的伙伴系统（新闻采集器）。采集器不在时，本功能自己报告
不可用，而不是让外壳崩掉——这正是插件契约里 ``check`` 的用途。
"""

from __future__ import annotations

from taa_futu.plugin import Availability, Feature, registry


def _check() -> Availability:
    from taa_futu.news_panel import news_root

    root = news_root()
    if not root.exists():
        return Availability(False, f"找不到新闻采集器：{root}（可用环境变量 JQ_NEWS_ROOT 指定）")
    report = root / "reports" / "live" / "latest_report.json"
    if not report.exists():
        return Availability(False, "采集器还没产出报告，跑一次 collect 即可")
    return Availability(True)


def _render(settings) -> None:
    from taa_futu.news_panel import render_news
    render_news(settings)


def _home_block() -> None:
    from taa_futu.news_panel import render_news_home_block
    render_news_home_block()


registry.register(Feature(
    id="news",
    label="市场新闻 / Market News",
    icon="📰",
    order=60,
    summary=(
        "采集 → 去重 → 聚类 → 规则打分 → AI 筛选 → 标的映射 → 手机推送。\n\n"
        "工作台保留原看板的点击跳转、卡片联动与拖拽问 AI。"
    ),
    render=_render,
    home_block=_home_block,
    check=_check,
    needs_settings=False,
    # 工作台自带页头与指标卡，外壳不要再画一层，否则两层头叠在一起、
    # 同样的数字显示两遍，新闻要滚过大半屏才看得到。
    meta={"full_bleed": True},
))
