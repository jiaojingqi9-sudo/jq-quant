"""实时建议功能。"""

from __future__ import annotations

from taa_futu.plugin import Feature, registry


def _render(settings) -> None:
    from taa_futu.dashboard_extras import render_live_signal
    render_live_signal(settings)


registry.register(Feature(
    id="live_signal",
    label="实时建议 / Live Signal",
    icon="🤖",
    order=40,
    summary="多 sleeve 综合评分查询，给出当下每个标的的建议方向与依据。",
    # 原本就是首页底部的快捷链接，key 沿用 enter_live（端到端测试按它点击）
    placement="quick",
    home_button_key="enter_live",
    render=_render,
))
