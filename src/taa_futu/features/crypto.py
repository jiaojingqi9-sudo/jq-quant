"""加密货币交易功能。"""

from __future__ import annotations

from taa_futu.plugin import Feature, registry


def _render(settings) -> None:
    from taa_futu.dashboard_extras import render_crypto_trading_full
    render_crypto_trading_full(settings)


registry.register(Feature(
    id="crypto",
    label="加密交易 / Crypto Trading",
    icon="💰",
    order=20,
    summary=(
        "Binance 现货 OFIM + USD-M 永续，完全独立于富途的另一条 sleeve。\n\n"
        "看：连接状态 / 账本 / 信号 / 订单；\n"
        "做：调币种池 / 改阈值 / 试算 / 模拟下单。"
    ),
    render=_render,
))
