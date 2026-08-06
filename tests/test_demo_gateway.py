"""演示模式的账户数字必须自洽。

这是外人 clone 下来打开的第一屏：总资产、现金、持仓市值、浮盈。
以前两个地方各算各的——`get_account_info` 用成本价算持仓市值、`get_positions`
用漂移后的现价，两处对不上；账户级浮盈干脆写死 1,234.56。加上总资产只有
72,125 而收益起点取的是 `initial_capital` 默认值 100 万，首页第一眼就是
「账户总盈亏 −927,875（−92.79%）」。演示看起来像亏光了。
"""
from types import SimpleNamespace

from taa_futu.demo_gateway import DEMO_ACC_ID, DEMO_TOTAL_ASSETS, DemoTrader


def _trader() -> DemoTrader:
    return DemoTrader(SimpleNamespace())


def test_demo_total_assets_matches_initial_capital_default() -> None:
    account = _trader().get_account_info(DEMO_ACC_ID)
    assert float(account["total_assets"]) == DEMO_TOTAL_ASSETS


def test_demo_account_and_positions_agree() -> None:
    trader = _trader()
    account = trader.get_account_info(DEMO_ACC_ID)
    positions = trader.get_positions(DEMO_ACC_ID)

    cash = float(account["cash"])
    market_val = float(account["market_val"])
    assert abs(cash + market_val - float(account["total_assets"])) < 0.01
    assert abs(market_val - float(positions["market_val"].sum())) < 0.01
    assert abs(float(account["unrealized_pl"]) - float(positions["unrealized_pl"].sum())) < 0.01


def test_demo_positions_are_reproducible() -> None:
    """同一个种子跑两次必须一模一样，否则每次刷新数字都在跳。"""
    first = _trader().get_positions(DEMO_ACC_ID)
    second = _trader().get_positions(DEMO_ACC_ID)
    assert list(first["nominal_price"]) == list(second["nominal_price"])
