"""练习一局：给你一个大单任务，你分批下，结束后算成绩。

回合制。每回合你决定这一回合下什么单，然后市场往前走一段时间（默认 30 秒），
期间机器人照常交易、你挂的限价单可能成交也可能没成交。回合制而不是实时，
是因为练的是「怎么切、什么时候用市价什么时候挂单」，不是手速。

成绩怎么算——这是整套东西的重点，先说清楚：

  到达价 (arrival)   你接到任务那一刻的中间价。跟它比才是真正的盈亏，
                     但一小时里价格自己就能走一个百分点，单看这个数
                     噪声大过本事。

  影子 VWAP          同一个种子再跑一遍、但你没进场的那个市场，在同一段
                     时间里的成交均价。这是及格线所在的指标。

                     为什么不用「真实市场这段时间的成交均价」当基准：你占了
                     一成成交量，基准就被你自己的成交价拉过去一成——砸得越狠
                     基准跟得越紧，分数反而越好看。更要命的是你把行情推走的
                     那部分（冲击、信息泄露）会同时推走基准，等于完全测不到。
                     模拟器有真实 TCA 没有的条件：可以把「你不在场的那个世界」
                     原样跑一遍。行情的涨跌两边一模一样，减完剩下的就纯粹是
                     你留下的脚印。

  参与率             你的成交量占同期市场总成交量的比例。太高说明你砸得
                     太急，冲击成本一定难看。

  被动成交占比       你的成交里有多少是挂单等来的（省了价差），有多少是
                     主动吃的（付了价差）。

正负号统一：正数＝比基准差（买贵了 / 卖便宜了）。
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from .book import BUY, SELL, Trade
from .market import MarketConfig, SyntheticMarket


@dataclass
class TaskSpec:
    """一个大单任务。"""
    symbol: str = "US.NVDA"
    side: int = BUY
    total_shares: int = 100_000
    horizon_sec: float = 3600.0     # 必须在这段时间内做完
    turn_sec: float = 30.0          # 一个回合推进多少秒
    # 没做完的部分怎么罚：收盘一把市价扫掉，按当时的价格算，再加这个惩罚。
    # 不罚的话「干脆不做」就成了最优解。
    unfilled_penalty_bp: float = 50.0


@dataclass
class Fill:
    ts: float
    price: float        # 真实价格，不是 tick
    qty: int
    passive: bool       # True＝挂单等来的，False＝主动吃的


@dataclass
class Report:
    filled: int
    target: int
    avg_price: float
    arrival_price: float
    market_vwap: float
    market_twap: float
    slip_vs_vwap_bp: float
    slip_vs_arrival_bp: float
    passive_rate: float
    participation_rate: float
    market_volume: int
    n_orders: int
    grade: str
    notes: list[str] = field(default_factory=list)


class Session:
    """一局练习。UI 层只跟这个类打交道。"""

    def __init__(self, task: TaskSpec | None = None, cfg: MarketConfig | None = None):
        self.task = task or TaskSpec()
        cfg = cfg or MarketConfig()
        self.market = SyntheticMarket(cfg)
        # 影子市场：同一个种子、同一段时间，但你不进场。用来当计分基准。
        self.shadow = SyntheticMarket(cfg)
        # 先空转一段，让盘口从「做市商刚开张」的状态进入常态，
        # 否则头几个回合的盘口是假的。两边必须走一样的步长。
        for _ in range(120):
            self.market.step(1.0)
            self.shadow.step(1.0)

        self.t0 = self.market.t
        self.arrival = self.market.book.mid() * self.market.cfg.tick
        self._trade_mark = len(self.market.book.trades)   # 任务开始时的成交序号
        self._shadow_mark = len(self.shadow.book.trades)
        self.fills: list[Fill] = []
        self.open_orders: dict[int, tuple[int, int]] = {}  # oid -> (整数价, 剩余股数)
        self.n_orders = 0
        self.finished = False

    # ── 状态 ─────────────────────────────────────────────────────────────
    @property
    def elapsed(self) -> float:
        return self.market.t - self.t0

    @property
    def remaining_sec(self) -> float:
        return max(0.0, self.task.horizon_sec - self.elapsed)

    @property
    def done_shares(self) -> int:
        return sum(f.qty for f in self.fills)

    @property
    def left_shares(self) -> int:
        return max(0, self.task.total_shares - self.done_shares)

    def avg_price(self) -> float:
        if not self.fills:
            return float("nan")
        return sum(f.price * f.qty for f in self.fills) / self.done_shares

    def market_trades(self) -> list[Trade]:
        """任务开始以来市场上所有的成交（含自己的）。"""
        return self.market.book.trades[self._trade_mark:]

    def board(self, levels: int = 10) -> dict:
        """价格阶梯要显示的东西。自己挂在哪一档也标出来。"""
        tick = self.market.cfg.tick
        mine: dict[int, int] = {}
        for oid, (px, _) in self.open_orders.items():
            o = self.market.book._index.get(oid)
            if o is not None:
                mine[px] = mine.get(px, 0) + o.qty
        b = [(self.market.book.to_price(p), q, mine.get(p, 0),
              self.market.book.level_qty(BUY, p) - mine.get(p, 0))
             for p, q in self.market.book.depth(BUY, levels)]
        a = [(self.market.book.to_price(p), q, mine.get(p, 0),
              self.market.book.level_qty(SELL, p) - mine.get(p, 0))
             for p, q in self.market.book.depth(SELL, levels)]
        return {
            "bid": b, "ask": a,
            "mid": self.market.book.mid() * tick if self.market.book.mid() else None,
            "spread_ticks": self.market.book.spread_ticks(),
            "arrival": self.arrival,
            "done": self.done_shares, "left": self.left_shares,
            "elapsed": self.elapsed, "remaining": self.remaining_sec,
            "avg_price": self.avg_price(),
        }

    # ── 下单 ─────────────────────────────────────────────────────────────
    def _record(self, fills: list[Trade], passive: bool) -> None:
        tick = self.market.cfg.tick
        for f in fills:
            self.fills.append(Fill(f.ts, f.price * tick, f.qty, passive))

    def send_market(self, qty: int) -> int:
        """主动吃单。返回成交股数。不会超过剩余任务量。"""
        qty = min(int(qty), self.left_shares)
        if qty <= 0:
            return 0
        self.n_orders += 1
        fills = self.market.player_market(self.task.side, qty)
        self._record(fills, passive=False)
        return sum(f.qty for f in fills)

    def send_limit(self, price: float, qty: int) -> int | None:
        """挂限价单。price 用真实价格（如 210.03）。返回单号，全成交则返回 None。

        挂得比对手价还激进的话会立刻成交一部分，那部分算主动吃。
        """
        qty = min(int(qty), self.left_shares - self._working_shares())
        if qty <= 0:
            return None
        self.n_orders += 1
        px = int(round(price / self.market.cfg.tick))
        oid, fills = self.market.player_limit(self.task.side, px, qty)
        self._record(fills, passive=False)     # 立刻成交的部分是自己穿过去的
        if oid is not None:
            self.open_orders[oid] = (px, qty - sum(f.qty for f in fills))
        return oid

    def cancel(self, oid: int) -> bool:
        ok = self.market.player_cancel(oid)
        self.open_orders.pop(oid, None)
        return ok

    def cancel_all(self) -> int:
        n = 0
        for oid in list(self.open_orders):
            if self.cancel(oid):
                n += 1
        return n

    def _working_shares(self) -> int:
        """还挂在簿里没成交的量。避免挂单总量超过任务量。"""
        tot = 0
        for oid in self.open_orders:
            o = self.market.book._index.get(oid)
            if o is not None:
                tot += o.qty
        return tot

    # ── 推进 ─────────────────────────────────────────────────────────────
    def advance(self, seconds: float | None = None) -> dict:
        """市场往前走一个回合。期间挂单成交了会自动记进来。"""
        if self.finished:
            return self.board()
        dt = self.task.turn_sec if seconds is None else seconds
        dt = min(dt, self.remaining_sec) if self.remaining_sec > 0 else 0.0
        before = len(self.market.book.trades)
        if dt > 0:
            self.market.step(dt)
            self.shadow.step(dt)
        # 挑出这段时间里自己挂单被成交的部分
        tick = self.market.cfg.tick
        for tr in self.market.book.trades[before:]:
            if tr.maker == "player":
                self.fills.append(Fill(tr.ts, tr.price * tick, tr.qty, True))
        # 清掉已经没了的单号
        for oid in list(self.open_orders):
            if oid not in self.market.book._index:
                self.open_orders.pop(oid)
        # 挂单量超过剩余任务量就撤掉多的（任务做完了还挂着会超量成交）
        if self._working_shares() > self.left_shares:
            self.cancel_all()
        if self.remaining_sec <= 0:
            self.finished = True
        return self.board()

    # ── 收工结算 ─────────────────────────────────────────────────────────
    def close_out(self) -> None:
        """时间到，没做完的部分一把市价扫掉。"""
        self.cancel_all()
        if self.left_shares > 0:
            self.send_market(self.left_shares)
        self.finished = True

    def report(self) -> Report:
        trades = self.market_trades()
        mkt_qty = sum(t.qty for t in trades)
        tick = self.market.cfg.tick
        # 基准只算「你实际在干活」的那段时间：干完就收工的话，后面的行情
        # 不该算进你的基准里。
        end = max((f.ts for f in self.fills), default=self.market.t)
        shadow = [t for t in self.shadow.book.trades[self._shadow_mark:] if t.ts <= end]
        sq = sum(t.qty for t in shadow)
        vwap = (sum(t.price * t.qty for t in shadow) / sq * tick) if sq else float("nan")
        # TWAP 用每笔成交价的简单平均近似，够用
        twap = (statistics.mean([t.price for t in shadow]) * tick) if shadow else float("nan")

        done = self.done_shares
        avg = self.avg_price()
        sign = 1.0 if self.task.side == BUY else -1.0
        notes: list[str] = []

        if done == 0:
            return Report(0, self.task.total_shares, float("nan"), self.arrival, vwap, twap,
                          float("nan"), float("nan"), 0.0, 0.0, mkt_qty, self.n_orders,
                          "未完成", ["一股都没成交"])

        slip_vwap = sign * (avg / vwap - 1) * 1e4
        slip_arr = sign * (avg / self.arrival - 1) * 1e4
        # 没做完的部分照罚，罚分直接加在两个滑点上
        short = self.task.total_shares - done
        if short > 0:
            miss = short / self.task.total_shares
            pen = miss * self.task.unfilled_penalty_bp
            slip_vwap += pen
            slip_arr += pen
            notes.append(f"少做了 {short:,} 股（{miss*100:.0f}%），按 {pen:.1f}bp 计罚")

        passive_qty = sum(f.qty for f in self.fills if f.passive)
        passive_rate = passive_qty / done
        # 市场总成交量里自己占两边，算参与率时要除以 2（一买一卖各记一次不算）
        part = done / mkt_qty if mkt_qty else 0.0

        # 分数线不是拍脑袋定的，是拿几种基准打法各跑 20 个随机日量出来的
        # （买 10 万股 / 1 小时 / 30 秒一回合，对影子 VWAP）：
        #   一把全打出去                  44.8bp
        #   每回合等额打市价               5.4bp
        #   每回合挂满等（不追）           5.9bp
        #   每回合只挂一小块 + 落后补市价   4.4bp
        #   挂单为主 + 落后补市价           1.0bp
        # 所以：7bp 是「没干蠢事」的线，1bp 以内说明确实会挑时机。
        if slip_vwap <= 1:
            grade = "优秀"
        elif slip_vwap <= 3:
            grade = "良好"
        elif slip_vwap <= 7:
            grade = "及格"
        else:
            grade = "不及格"

        if part > 0.25:
            notes.append(f"参与率 {part*100:.0f}%，砸得太急了——一般不超过 20%")
        if passive_rate < 0.2:
            notes.append(f"被动成交只占 {passive_rate*100:.0f}%，几乎全是主动吃单，价差白付了")
        return Report(done, self.task.total_shares, avg, self.arrival, vwap, twap,
                      slip_vwap, slip_arr, passive_rate, part, mkt_qty, self.n_orders,
                      grade, notes)
