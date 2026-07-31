"""历史模拟 / 回测功能。"""

from __future__ import annotations

from taa_futu.plugin import Feature, registry


def _render(settings) -> None:
    from taa_futu.dashboard_app import render_historical_simulation
    from taa_futu.dashboard_extras import render_nav_breadcrumb

    render_nav_breadcrumb("📊 历史模拟 / Historical Simulation")
    render_historical_simulation(settings)


registry.register(Feature(
    id="stock_history",
    label="历史模拟 / Historical Sim",
    icon="📊",
    order=70,
    summary="用历史数据回放策略，检验参数与假设。",
    render=_render,
))
