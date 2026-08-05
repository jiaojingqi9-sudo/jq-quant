"""下单练习台。

用自己收集的 NVDA 盘口数据标定出一个合成市场，在上面练大单的切分与下单，
收工后按「你不在场的那个市场」的成交均价打分。

不依赖任何外部系统（不连富途、不读实时行情），所以永远可用。
"""

from __future__ import annotations

from taa_futu.plugin import Availability, Feature, registry


def _check() -> Availability:
    return Availability(True)


def _render(settings) -> None:
    from taa_futu.exec_trainer.panel import render_exec_trainer
    render_exec_trainer(settings)


registry.register(Feature(
    id="exec_trainer",
    label="下单练习 / Execution Trainer",
    icon="🎯",
    order=70,
    summary=(
        "大单执行练习。合成市场的价差分布、前 20 档深度和 5 分钟波动率"
        "都对着自己收集的 NVDA 真实盘口标定过。\n\n"
        "评分基准是同一个种子、你没进场的那个市场——冲击成本和信息泄露藏不住。"
    ),
    render=_render,
    check=_check,
    needs_settings=False,
))
