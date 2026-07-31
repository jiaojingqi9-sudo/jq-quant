"""选股器功能。"""

from __future__ import annotations

from taa_futu.plugin import Feature, registry


def _render(settings) -> None:
    from taa_futu.dashboard_extras import render_screener_full
    render_screener_full(settings)


registry.register(Feature(
    id="screener",
    label="选股器 / Screener",
    icon="🔍",
    order=30,
    summary=(
        "多因子在线筛选 + AH 多因子扫描。\n\n"
        "用四 sleeve 的实时评分对 universe 排序，也能跑 AH 连板 / 缩量上涨 / "
        "接近新高扫描。"
    ),
    render=_render,
))
