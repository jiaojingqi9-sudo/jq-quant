"""合成市场引擎：复刻 TraderEx 的做法，参数用自己的真实数据标定。

市场由一个看不见的「真实价值」推着走，四类机器人围着它下单：

  做市商    两边挂梯子，决定盘口长什么样（价差多宽、每档多厚）
  噪音      双边随机下单，跟价格无关，提供基础流动性
  价值      看得到带噪声的真实价值，价格偏离就来纠正——价格发现靠它
  动量      追短期趋势，把行情放大

TraderEx 只有后三类。加做市商是因为它那套撑不出 NVDA 的盘口形状：
真实的 NVDA 是「最优档薄、第 3–6 档最厚」的驼峰形，随机限价单堆不出来，
那是做市商挂梯子挂出来的。

价格过程同时用两种：TraderEx 是纯跳跃（模拟消息），ABIDES 是均值回复
（模拟日常波动）。真实的一天两者都有，所以叠加。

—— 一条重要的建模约定 ——
盘口最前面那一档（买一/卖一）只由做市商摆。别人要么主动吃掉它，要么排在
它后面。真实市场当然人人都能在买一挂单，但那样建模跑出来的结果是错的：
第一版就是让噪音单也挂在买一，结果买一堆到目标的两倍、价差被永久压死在
1 tick（实测买一 246 股 vs 目标 125，价差 1 tick 占 82% vs 目标 40%）。
练习者需要感觉真实的是排队位置、盘口厚度和冲击成本，不是「谁在买一挂单」
的人口普查，所以这里选简化。
"""
from __future__ import annotations

import heapq
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

from .book import BUY, SELL, Book, Trade


# ── 真实价值 ─────────────────────────────────────────────────────────────

@dataclass
class Fundamental:
    """看不见的「这只股票到底值多少」。玩家看不到，教师端能看能推。

    连续部分：向当日中枢缓慢回归 + 随机扰动（日常波动）
    跳跃部分：泊松到达的台阶（消息）
    """

    price: float                 # 当前真实价值（真实价格，不是 tick）
    anchor: float                # 回归中枢
    sigma_per_sec: float         # 每秒波动
    kappa: float                 # 回归速度，越大拉得越紧
    jump_rate_per_sec: float     # 每秒跳跃概率
    jump_size: float             # 跳跃幅度（相对值，如 0.0013 = 13bp）
    rng: random.Random

    def step(self, dt: float) -> float:
        """推进 dt 秒，返回这一步的跳跃幅度（0 表示没跳）。"""
        # 均值回复 + 随机游走
        drift = self.kappa * (self.anchor - self.price) * dt
        shock = self.rng.gauss(0.0, self.sigma_per_sec * math.sqrt(max(dt, 0.0)) * self.price)
        self.price += drift + shock
        # 跳跃
        jumped = 0.0
        if self.rng.random() < self.jump_rate_per_sec * dt:
            # 幅度用指数分布，中位数对上标定值；方向五五开
            size = self.rng.expovariate(1.0 / max(self.jump_size, 1e-9)) * math.log(2)
            jumped = size if self.rng.random() < 0.5 else -size
            self.price *= (1.0 + jumped)
        return jumped

    def nudge(self, rel: float) -> None:
        """教师端手动推一下（Increase / Decrease P*）。"""
        self.price *= (1.0 + rel)


# ── 参数 ─────────────────────────────────────────────────────────────────

@dataclass
class MarketConfig:
    tick: float = 0.01
    start_price: float = 210.0
    # 盘口形状（单边各档目标股数），做市商照这个挂。
    # 这是 NVDA 2026-07-28 至 08-04 六个交易日、盘中时段的各档中位数。
    depth_profile: list[int] = field(default_factory=lambda: [
        125, 356, 411, 408, 409, 414, 348, 314, 287, 294,
        281, 276, 267, 247, 247, 272, 296, 300, 268, 232,
    ])
    # 第 20 档以后再往外铺多少档，每档按第 20 档的量挂。
    # 标定只量到第 20 档，再往外是外推、不是实测。加这段纯粹是为了簿不会被
    # 一张大单打空：只铺 20 档的话单边总共才六千股，玩家一次打五千股就能把
    # 卖盘扫光、盘口变成空的，后面的撮合和计价全乱。
    mm_tail_levels: int = 60
    # 深水区每隔几个 tick 挂一笔（一笔顶几档的量）。只影响那一段的颗粒度，
    # 不影响标定过的前 20 档。
    mm_tail_stride: int = 4
    # 价差分布：几个 tick -> 概率（同样是那六天的实测）
    spread_dist: dict[int, float] = field(default_factory=lambda: {
        1: 0.397, 2: 0.451, 3: 0.130, 4: 0.016, 5: 0.003,
    })
    sigma_per_sec: float = 0.00016117
    # 均值回复速度。标定值是 0.0018（半衰期约 6.4 分钟），但那个值是从
    # 「已经含了均值回复的真实价格序列」上量出来的，sigma 也是从同一条序列上
    # 量的。把两个都原样喂进 OU 过程等于把回复算了两遍，5 分钟波动会被压掉
    # 三成。这里把回复放松到半衰期约 25 分钟：一天之内价格不会飘到离谱的地方，
    # 又不吃掉 5 分钟尺度上的波动。
    kappa: float = 0.00045
    jumps_per_day: float = 17.5
    jump_abs_median_bp: float = 12.89
    # 标定出来的「盘口每秒有几档的挂单量发生变化」。注意这个数不是
    # 「每秒来几张新单」——40 个档位每秒各变一次就是 50，而做市商自己每秒
    # 重挂一次梯子就能造出这么多变化。第一版误当成到达率用，结果簿被堆到
    # 目标的两三倍。现在它只作为记录保留，实际的下单流由下面那个参数控制。
    order_events_per_sec: float = 49.9
    order_size_median: float = 47.5
    order_size_p90: float = 207.5
    # 真正的机器人下单频率（每秒几张）。做市商之外的三类共用这个速率。
    # 定这个数的依据：稳态下非做市商的挂单存量应该只占目标深度的四分之一
    # 左右（做市商占 mm_fill_ratio），即每档每边约一张单。
    agent_events_per_sec: float = 5.0
    order_size_p99: float = 738.0
    # 抽到大单的概率。标定的 p99 就是「百分之一的单子有这么大」。
    big_order_prob: float = 0.01
    # 机器人配比。做市商单独一个，其余三类按这个比例瓜分订单流。
    mix_noise: float = 0.55
    mix_value: float = 0.30
    mix_momentum: float = 0.15
    # 噪音单里有多大比例直接打市价
    noise_market_prob: float = 0.25
    # 噪音限价单挂在本方最优价后面第几档（1 到这个数之间均匀取）
    noise_depth_levels: int = 5
    # 价值型有多大概率直接穿价差吃单（ABIDES 的 percent_aggr）
    value_aggressive_prob: float = 0.10
    # 做市商挂的量占目标剖面的比例。其余深度由其他机器人的挂单自然堆出来。
    # 实测其他机器人在最优档能堆出三成多、深档一成左右，所以这里不到 1。
    mm_fill_ratio: float = 0.98
    # 每档挂单量的离散度（对数正态的 sigma）。不加这个，每一档永远是同一个数，
    # 盘口看起来像画上去的；真实数据每档的四分位距很宽。
    # 0.35 是暂定值，等标定档案导出 p25/p75 之后按 ln(p75/p25)/1.349 换算。
    depth_dispersion: float = 0.35
    mm_requote_sec: float = 0.5
    # 做市商每次醒来有多大概率重抽一次价差。价差有粘性——真实市场的价差是
    # 成片的，不是每秒随机跳。这个概率只影响价差的持续时间，不影响它的分布。
    mm_spread_change_prob: float = 0.25
    # 库存偏斜的尺度：手里净头寸到这个量级时，报价才会明显歪
    mm_inventory_scale: float = 5000.0

    # ── 玩家下单对价格的推动 ────────────────────────────────────────────
    # 这是练习系统的核心，没有它整套东西就是假的：不加冲击，价格只跟着
    # 看不见的真实价值走，玩家怎么砸都会立刻弹回去，那最优解就变成
    # 「一把全打出去然后等」——现实里恰恰相反。
    #
    # 分两块：
    #   永久   市场从你的成交里推断出「有人知道点什么」，价格不再回去
    #   临时   你把对手盘吃薄了，价格暂时被推开，别人补上来之后慢慢弹回
    # 只对玩家主动吃单的量生效。机器人的成交不另外加冲击——它们造成的
    # 价格波动已经包含在标定出来的波动率里了，再加一遍就是重复计算。
    impact_perm_per_share: float = 8e-9      # 10 万股 → 约 8bp 永久推动
    impact_temp_per_share: float = 5e-8      # 2 万股一把打 → 约 10bp 临时推开
    impact_half_life_sec: float = 60.0       # 临时部分的半衰期

    # ── 信息泄露：把大单明晃晃挂在盘口上要付的代价 ──────────────────────
    # 没有这一条，第一版跑出来「全部挂在买一等着」几乎零成本、稳赢，
    # 那就把练习变成了送分题。真实市场不是这样：你在买一挂出十倍于平常的量，
    # 别人一眼就看出有人急着买一大笔，就抢在你前面挂高一个 tick——反正
    # 你还得接着买，他挂在你前面稳赚。结果是你排在后面成交不了，只能跟着
    # 往上追，一追别人再抢一次。这是做大单的人最真实的痛点，必须在模型里。
    mm_frontrun_ratio: float = 2.0        # 露出的量超过最优档平常厚度的几倍算「露了」
    mm_frontrun_watch_ticks: int = 3      # 只盯着盘口附近这几档，挂得很深的不算威胁
    mm_frontrun_max_ticks: int = 3        # 最多抢到公允价上方几个 tick 为止
    # 非做市商的限价单平均活多久就撤掉（秒）。
    # 这条不能省：真实盘口的厚度是「挂了又撤」之后的净存量，
    # 不撤单的话簿会一直变厚，跑半小时就厚到目标的两三倍、价差被压到永远 1 tick。
    order_life_sec: float = 5.0
    # 真实价值按固定的时间网格推进，不跟着事件走。
    # 这一条是为了能算「没有你的话，市场会是什么样」：只有价格路径跟玩家
    # 的操作无关，才能拿同一个种子再跑一遍空市场当基准。跟着事件推进的话，
    # 你多下一张单就会改变随机数的消耗顺序，两次跑出来的行情完全不是一回事，
    # 基准也就没意义了。
    fundamental_grid_sec: float = 0.25
    seed: int = 1

    @classmethod
    def from_profile(cls, path: str | Path, **overrides) -> "MarketConfig":
        """读标定档案。没有档案就用类里的默认值（那是 NVDA 六天的中位数）。"""
        p = Path(path)
        if not p.exists():
            return cls(**overrides)
        prof = json.loads(p.read_text(encoding="utf-8"))
        dist = {}
        for k, v in (prof.get("spread_ticks_dist") or {}).items():
            if k.isdigit():
                dist[int(k)] = float(v)
        kw = dict(
            tick=float(prof.get("tick_size", 0.01)),
            depth_profile=[int(round(x)) for x in prof.get("depth_profile_shares", [])] or None,
            spread_dist=dist or None,
            sigma_per_sec=float(prof.get("sigma_per_sec", 0.00016117)),
            jumps_per_day=float(prof.get("jumps_per_day", 17.5)),
            jump_abs_median_bp=float(prof.get("jump_abs_median_bp", 12.89)),
            order_events_per_sec=float(prof.get("order_events_per_sec", 49.9)),
            order_size_median=float(prof.get("order_size_median", 47.5)),
            order_size_p90=float(prof.get("order_size_p90", 207.5)),
            order_size_p99=float(prof.get("order_size_p99", 738.0)),
        )
        # kappa 故意不从档案里取，理由见上面 kappa 字段的注释。
        kw = {k: v for k, v in kw.items() if v is not None}
        kw.update(overrides)
        return cls(**kw)


# ── 引擎 ─────────────────────────────────────────────────────────────────

class SyntheticMarket:
    """离散事件推进。每个机器人有自己的下次醒来时间，取最早的那个往前走。"""

    def __init__(self, cfg: MarketConfig):
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        self.book = Book(tick=cfg.tick)
        self.t = 0.0

        self.fundamental = Fundamental(
            price=cfg.start_price,
            anchor=cfg.start_price,
            sigma_per_sec=cfg.sigma_per_sec,
            kappa=cfg.kappa,
            # 一天按 6.5 小时算
            jump_rate_per_sec=cfg.jumps_per_day / (6.5 * 3600),
            jump_size=cfg.jump_abs_median_bp / 1e4,
            rng=random.Random(cfg.seed + 1),
        )

        # 订单大小：对数正态，中位数和 p90 对上标定值
        mu = math.log(max(cfg.order_size_median, 1.0))
        sigma = (math.log(max(cfg.order_size_p90, 2.0)) - mu) / 1.2816
        self._size_mu, self._size_sigma = mu, max(sigma, 0.05)

        self.mm = MarketMaker(self, cfg)
        self._expiry: list[tuple[float, int]] = []      # (到期时间, 单号)
        self._events: list[tuple[float, int, str]] = []
        self._seq = 0
        self._mom_prices: list[float] = []
        self._fund_t = 0.0
        self.inventory: dict[str, int] = {"player": 0}
        # 玩家造成的临时冲击，会随时间衰减回 0
        self._impact = 0.0
        self._impact_decay = math.log(2) / max(cfg.impact_half_life_sec, 1e-6)

        self._bootstrap()

    # ── 建簿 ─────────────────────────────────────────────────────────────
    def _bootstrap(self) -> None:
        """开局先让做市商把两边梯子挂上，否则第一张单没有对手。"""
        self.mm.requote()
        self._push(self.cfg.mm_requote_sec, "mm")
        self._push(self._next_order_gap(), "flow")

    def _push(self, dt: float, who: str) -> None:
        self._seq += 1
        heapq.heappush(self._events, (self.t + max(dt, 1e-6), self._seq, who))

    def _next_order_gap(self) -> float:
        """泊松到达＝间隔服从指数分布。无记忆，玩家没法靠节奏预测。"""
        rate = max(self.cfg.agent_events_per_sec, 1e-6)
        return self.rng.expovariate(rate)

    def _draw_size(self) -> int:
        """大部分是对数正态的小单，百分之一是大单。

        两段式是有意的：只用对数正态的话，尾巴对不上标定的 p99，玩家永远
        碰不到「一张大单把前几档扫掉」的场面——而那正是做大单的人最需要
        练的一课（自己的单子被别人的大单顶走是什么感觉）。
        """
        if self.rng.random() < self.cfg.big_order_prob:
            return max(1, int(self.cfg.order_size_p99 * self.rng.uniform(0.6, 1.6)))
        return max(1, int(round(self.rng.lognormvariate(self._size_mu, self._size_sigma))))

    def _rest(self, oid: int | None) -> None:
        """机器人的限价单挂进簿之后，登记一个到期时间，到点自动撤。"""
        if oid is None:
            return
        life = self.rng.expovariate(1.0 / max(self.cfg.order_life_sec, 0.1))
        heapq.heappush(self._expiry, (self.t + life, oid))

    def _expire(self, until: float) -> None:
        while self._expiry and self._expiry[0][0] <= until:
            _, oid = heapq.heappop(self._expiry)
            self.book.cancel(oid)

    # ── 推进 ─────────────────────────────────────────────────────────────
    def step(self, dt: float) -> list[Trade]:
        """往前走 dt 秒。返回这段时间里发生的成交。"""
        target = self.t + dt
        start_trades = len(self.book.trades)
        while self._events and self._events[0][0] <= target:
            when, _, who = heapq.heappop(self._events)
            self._advance_fundamental(when)
            self.t = when
            self._expire(when)
            if who == "mm":
                self.mm.requote()
                self._push(self.cfg.mm_requote_sec, "mm")
            else:
                self._one_order()
                # 大单把做市商最优档扫空了就当场补，不等下一个时钟点。
                # 真实做市商是按事件反应的，不是按秒；不这样做，簿被扫空之后
                # 会有半秒钟没有买一卖一，中间价算不出来。
                if self.mm.touch_empty():
                    self.mm.requote()
                self._push(self._next_order_gap(), "flow")
        self._advance_fundamental(target)
        self.t = target
        self._expire(target)
        # 临时冲击按半衰期衰减回去
        self._impact *= math.exp(-self._impact_decay * dt)
        self._record_mid()
        new = self.book.trades[start_trades:]
        self._apply_fills(new)
        return new

    def _advance_fundamental(self, until: float) -> None:
        """把真实价值推进到 until。按固定网格走，跟事件无关（理由见配置里的注释）。"""
        g = self.cfg.fundamental_grid_sec
        while self._fund_t + g <= until + 1e-9:
            self._fund_t += g
            self.fundamental.step(g)

    def _apply_fills(self, fills: list[Trade]) -> None:
        """成交要记到持仓上，玩家主动吃的量还要推一下价格。"""
        for tr in fills:
            # 挂单方与吃单方方向相反：吃单方是买，挂单方就是卖
            if tr.maker in ("mm", "mmtail"):
                self.mm.inventory -= tr.qty * tr.taker_side
            elif tr.maker == "player":
                self.inventory["player"] -= tr.qty * tr.taker_side
            if tr.taker == "player":
                self.inventory["player"] += tr.qty * tr.taker_side
                signed = tr.qty * tr.taker_side
                self.fundamental.price *= (1.0 + self.cfg.impact_perm_per_share * signed)
                self._impact += self.cfg.impact_temp_per_share * signed

    def fair_ticks(self) -> float:
        """做市商眼里的公允价：真实价值 + 玩家推开的那部分。"""
        return self.fundamental.price * (1.0 + self._impact) / self.cfg.tick

    def _reprice_bots(self, best_bid: int, best_ask: int) -> None:
        """把机器人挂在做市商报价里面的旧单撤掉。玩家的单子不碰。"""
        for o in list(self.book._index.values()):
            if o.owner in ("mm", "mmtail", "player"):
                continue
            if (o.side == BUY and o.price > best_bid) or (o.side == SELL and o.price < best_ask):
                self.book.cancel(o.oid)

    def _record_mid(self) -> None:
        m = self.book.mid()
        if m is not None:
            self._mom_prices.append(m)
            if len(self._mom_prices) > 200:
                del self._mom_prices[:-200]

    # ── 三类机器人各下一张单 ─────────────────────────────────────────────
    def _one_order(self) -> None:
        r = self.rng.random()
        if r < self.cfg.mix_noise:
            self._noise_order()
        elif r < self.cfg.mix_noise + self.cfg.mix_value:
            self._value_order()
        else:
            self._momentum_order()

    def _noise_order(self) -> None:
        """双边随机，跟价格无关——模拟「我就是要用钱」的人。"""
        side = BUY if self.rng.random() < 0.5 else SELL
        qty = self._draw_size()
        b, a = self.book.best_bid(), self.book.best_ask()
        if b is None or a is None:
            return
        if self.rng.random() < self.cfg.noise_market_prob:
            self.book.market(side, qty, "noise", self.t)
        else:
            # 挂在本方最优价「后面」1–5 档。不挂在最优价上：那一档是做市商的
            # 位置，别人想在那儿成交就得主动吃（那是价值型和动量型干的事）。
            off = self.rng.randint(1, max(1, self.cfg.noise_depth_levels))
            px = b - off if side == BUY else a + off
            oid, _ = self.book.limit(side, px, qty, "noise", self.t)
            self._rest(oid)

    def _value_order(self) -> None:
        """看得到带噪声的真实价值。这是价格发现的来源。

        照 ABIDES 的 ValueAgent：不直接知道真实价值，只有一个带噪观测。
        观测偏差按当前波动率缩放——市场越乱，看得越不准。
        """
        b, a = self.book.best_bid(), self.book.best_ask()
        if b is None or a is None:
            return
        noise = self.rng.gauss(0.0, self.fundamental.price * self.cfg.sigma_per_sec * 30)
        belief = (self.fundamental.price + noise) / self.cfg.tick
        qty = self._draw_size()

        # TraderEx 的规则：真实价值跑到买卖价之外时，明显更爱用市价单
        outside = belief > a or belief < b
        aggressive = self.rng.random() < (0.70 if outside else self.cfg.value_aggressive_prob)

        if belief > a:                      # 觉得便宜 → 买
            if aggressive:
                self.book.market(BUY, qty, "value", self.t)
            else:
                self._rest(self.book.limit(BUY, b - 1, qty, "value", self.t)[0])
        elif belief < b:                    # 觉得贵 → 卖
            if aggressive:
                self.book.market(SELL, qty, "value", self.t)
            else:
                self._rest(self.book.limit(SELL, a + 1, qty, "value", self.t)[0])
        else:
            # 价格在合理区间内，排在做市商后面等着
            side = BUY if self.rng.random() < 0.5 else SELL
            px = b - 1 if side == BUY else a + 1
            self._rest(self.book.limit(side, px, qty, "value", self.t)[0])

    def _momentum_order(self) -> None:
        """追动量：短均线在长均线上方就买。用限价单挂对手价，比市价单温和。"""
        if len(self._mom_prices) < 50:
            return
        short = sum(self._mom_prices[-20:]) / 20
        long = sum(self._mom_prices[-50:]) / 50
        b, a = self.book.best_bid(), self.book.best_ask()
        if b is None or a is None:
            return
        qty = self._draw_size()
        # 用 ioc：动量型是冲着吃单去的，吃不满就算了。留在对手价上会把价差压死。
        if short >= long:
            self.book.limit(BUY, a, qty, "momentum", self.t, ioc=True)
        else:
            self.book.limit(SELL, b, qty, "momentum", self.t, ioc=True)

    # ── 玩家接口 ─────────────────────────────────────────────────────────
    def player_market(self, side: int, qty: int) -> list[Trade]:
        fills = self.book.market(side, qty, "player", self.t)
        self._apply_fills([f for f in fills if f.maker != "player"])
        return fills

    def player_limit(self, side: int, price_ticks: int, qty: int) -> tuple[int | None, list[Trade]]:
        oid, fills = self.book.limit(side, price_ticks, qty, "player", self.t)
        self._apply_fills(fills)
        return oid, fills

    def player_cancel(self, oid: int) -> bool:
        return self.book.cancel(oid)


class MarketMaker:
    """两边挂梯子，决定盘口的形状。

    照 ABIDES 的自适应做市商，但每档挂多少直接照标定出来的深度剖面来——
    我们有真实数据，不必像它那样用「近期成交量的 5%」去猜。

    醒来时分两种情况：报价该动了就整条梯子撤掉重挂；报价没变就只把被吃掉的
    部分补回来。这样做不只是省事——补回来的量排在队尾，玩家挂在前面的单子
    不会因为做市商刷新而丢掉排队位置。
    """

    def __init__(self, market: "SyntheticMarket", cfg: MarketConfig):
        self.m = market
        self.cfg = cfg
        self.inventory = 0
        self._spread: int | None = None
        self._bid: int | None = None
        self._ask: int | None = None
        # 梯子的每一级：(离最优价几个 tick, 买边挂多少, 卖边挂多少)
        self._rungs: list[tuple[int, int, int]] = []
        self._profile = list(cfg.depth_profile)

    def _draw_spread(self) -> int:
        """按标定的价差分布抽一个价差（单位：tick）。"""
        r = self.m.rng.random()
        acc = 0.0
        for ticks, p in sorted(self.cfg.spread_dist.items()):
            acc += p
            if r <= acc:
                return max(1, ticks)
        return 2

    def touch_empty(self) -> bool:
        """自己的最优档还在不在。被扫空了就要立刻重挂。"""
        if self._bid is None or self._ask is None:
            return True
        return (self.m.book.owner_qty(BUY, self._bid, "mm") == 0
                or self.m.book.owner_qty(SELL, self._ask, "mm") == 0)

    def _frontrun(self, raw_bid: int, raw_ask: int, spread: int) -> tuple[int, int]:
        """玩家在盘口上露了大单，就抢在他前面挂高（低）一个 tick。

        抢价有上限：再怎么抢也不会离公允价太远，否则做市商自己要亏。
        玩家把单子撤掉或者挂得很深，这里立刻就不生效了——藏好了就没有这份罚。
        """
        thresh = self.cfg.depth_profile[0] * self.cfg.mm_frontrun_ratio
        watch = self.cfg.mm_frontrun_watch_ticks
        cap = self.cfg.mm_frontrun_max_ticks
        qb = qa = 0
        top_bid: int | None = None
        top_ask: int | None = None
        for o in self.m.book.orders_of("player"):
            if o.side == BUY and o.price >= raw_bid - watch:
                qb += o.qty
                top_bid = o.price if top_bid is None else max(top_bid, o.price)
            elif o.side == SELL and o.price <= raw_ask + watch:
                qa += o.qty
                top_ask = o.price if top_ask is None else min(top_ask, o.price)
        if qb > thresh and top_bid is not None:
            bid = min(top_bid + 1, raw_bid + cap)
            if bid > raw_bid:
                return bid, bid + spread
        if qa > thresh and top_ask is not None:
            ask = max(top_ask - 1, raw_ask - cap)
            if ask < raw_ask:
                return ask - spread, ask
        return raw_bid, raw_ask

    def _draw_ladder(self) -> list[tuple[int, int, int]]:
        """每次挪窝重抽一次梯子。中位数就是标定的目标剖面。

        标定到的那 20 档一档一档挂。再往外是外推出来的深水区，那一段
        隔几个 tick 才挂一笔、一笔顶几档的量：它存在的唯一目的是让簿不会被
        一张离谱的大单打空，形状对不对无所谓，而每档都挂会让整个模拟慢一倍。
        """
        def q(target: float) -> int:
            return max(1, int(target * math.exp(self.m.rng.gauss(0.0, self.cfg.depth_dispersion))))

        rungs = []
        for i, target in enumerate(self._profile):
            base = target * self.cfg.mm_fill_ratio
            rungs.append((i, q(base), q(base)))
        deep = self._profile[-1] * self.cfg.mm_fill_ratio * self.cfg.mm_tail_stride
        start = len(self._profile)
        for off in range(start, start + self.cfg.mm_tail_levels, self.cfg.mm_tail_stride):
            rungs.append((off, q(deep), q(deep)))
        return rungs

    def requote(self) -> None:
        fair = self.m.fair_ticks()
        if self._spread is None or self.m.rng.random() < self.cfg.mm_spread_change_prob:
            self._spread = self._draw_spread()
        spread = self._spread
        # 库存偏斜：手里多了就把报价整体压低，逼自己卖出去。
        # 用 tanh 是为了偏斜有上限，不会因为一次大成交就把报价推到离谱的地方。
        skew = math.tanh(self.inventory / self.cfg.mm_inventory_scale) * spread
        # 先定买价再加价差，保证挂出来的价差正好等于抽到的那个数——
        # 这样价差的边际分布是照标定构造出来的，不靠碰运气。
        best_bid = int(math.floor(fair - spread / 2 - skew))
        best_ask = best_bid + spread
        best_bid, best_ask = self._frontrun(best_bid, best_ask, spread)

        if (best_bid, best_ask) != (self._bid, self._ask):
            self.m.book.cancel_all("mm")
            self._bid, self._ask = best_bid, best_ask
            self._rungs = self._draw_ladder()
            for off, qb, qa in self._rungs:
                self.m.book.limit(BUY, best_bid - off, qb, "mm", self.m.t)
                self.m.book.limit(SELL, best_ask + off, qa, "mm", self.m.t)
            # 报价挪了位置，别的机器人留在里面的旧单要撤——真人会改价，
            # 不会把一张已经跑到盘口最里面的旧单干晾着。不撤的话那些旧单
            # 就成了买一卖一，价差被它们钉死在 1 tick。玩家的单子不动，
            # 「行情跑了自己的挂单还杵在那儿」正是要练的东西。
            self.m._reprice_bots(best_bid, best_ask)
        else:
            for off, qb, qa in self._rungs:
                for side, px, qty in ((BUY, best_bid - off, qb), (SELL, best_ask + off, qa)):
                    have = self.m.book.owner_qty(side, px, "mm")
                    if have < qty:
                        self.m.book.limit(side, px, qty - have, "mm", self.m.t)

