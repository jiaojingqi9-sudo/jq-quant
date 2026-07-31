"""股票交易功能。"""

from __future__ import annotations

from taa_futu.plugin import Feature, registry


def _render(settings) -> None:
    # 延迟导入：dashboard_app 很大且会拉起行情依赖，只有真的进这个页才付代价。
    from taa_futu.dashboard_app import render_live_monitor
    from taa_futu.dashboard_extras import render_nav_breadcrumb

    render_nav_breadcrumb("📈 股票交易 / Stock Trading")
    render_live_monitor(settings)


registry.register(Feature(
    id="stock",
    label="股票交易 / Stock Trading",
    icon="📈",
    order=10,
    summary=(
        "TAA + Fusion + OFIM + Cascade 四 sleeve 量化 stack，模拟盘自动运行。\n\n"
        "看：实时监控 / 持仓 / 订单 / 日内信号；\n"
        "做：启停自动运行 / pre-gate 切换 / 调整 stack 权重。"
    ),
    render=_render,
))
