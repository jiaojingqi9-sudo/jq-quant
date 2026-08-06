"""连续竞价订单簿：价格优先、同价先到先得。

这是练习系统的地基。合成市场和真实回放两个引擎都往这上面撮合，
练习者的单子和机器人的单子走完全一样的规则——这正是合成市场比录像回放强的地方：
你吃掉一档，做市商下次重挂时看到的量就变了，市场真的会对你有反应。

只做连续竞价。集合竞价、做市商报价、暗池留到后面的阶段。

性能上有两处不能省：最优价缓存、按主人分组的索引。做市商一次重挂要动上百张单，
每张都去 min()/max() 一遍整个价格字典的话，跑一小时要两分钟；加上这两处之后是两秒。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Iterable


BUY = 1
SELL = -1


@dataclass
class Order:
    oid: int
    side: int          # BUY / SELL
    price: int         # 以「几个最小变动价位」计的整数价，避免浮点比较出错
    qty: int           # 还没成交的总量（含没露出来的部分）
    owner: str         # 谁下的：mm / inst / noise / value / momentum / player
    ts: float
    # 露出多少。冰山单／隐藏单：盘口上只看得见 display 这么多，
    # 但排队和成交按 qty 全额算。
    #
    # 这不是为了炫技，是两组实测数据对不上时唯一的解释：
    # 富途盘口量出来买一中位数只有 125 股，而 K 线量出来 NVDA 一天成交
    # 七千万股（每秒 3027 股）。125 股的盘口撑不起每秒 3000 股的成交，
    # 除非绝大部分挂单根本没显示出来。显示出来的只是冰山一角。
    #
    # 对练习者来说这是最要紧的一课：盘口显示买一 125 股、你排第二个，
    # 不代表马上就轮到你——前面还压着看不见的量。
    display: int = 10**9


@dataclass
class Trade:
    ts: float
    price: int
    qty: int
    taker: str
    maker: str
    taker_side: int


@dataclass
class Book:
    """价格用整数 tick 表示。对外换算成真实价格由调用方负责。"""

    tick: float = 0.01
    _bids: dict[int, Deque[Order]] = field(default_factory=dict)
    _asks: dict[int, Deque[Order]] = field(default_factory=dict)
    _index: dict[int, Order] = field(default_factory=dict)          # oid -> Order
    _by_owner: dict[str, set[int]] = field(default_factory=dict)    # owner -> {oid}
    _next_oid: int = 1
    trades: list[Trade] = field(default_factory=list)
    # 最优价缓存。None 表示「没有」，_bb_dirty 表示「缓存失效了，要重算」。
    _bb: int | None = None
    _ba: int | None = None
    _bb_dirty: bool = False
    _ba_dirty: bool = False

    # ── 查询 ─────────────────────────────────────────────────────────────
    def best_bid(self) -> int | None:
        if self._bb_dirty:
            self._bb = max(self._bids) if self._bids else None
            self._bb_dirty = False
        return self._bb

    def best_ask(self) -> int | None:
        if self._ba_dirty:
            self._ba = min(self._asks) if self._asks else None
            self._ba_dirty = False
        return self._ba

    def mid(self) -> float | None:
        b, a = self.best_bid(), self.best_ask()
        if b is None or a is None:
            return None
        return (b + a) / 2

    def spread_ticks(self) -> int | None:
        b, a = self.best_bid(), self.best_ask()
        return None if b is None or a is None else a - b

    def depth(self, side: int, levels: int) -> list[tuple[int, int]]:
        """从最优价往里数 levels 档，返回 [(整数价, 看得见的量)]。"""
        side_book = self._bids if side == BUY else self._asks
        prices = sorted(side_book, reverse=(side == BUY))[:levels]
        return [(p, sum(min(o.qty, o.display) for o in side_book[p])) for p in prices]

    def level_qty(self, side: int, price: int) -> int:
        """这一档盘口上显示多少股。"""
        side_book = self._bids if side == BUY else self._asks
        return sum(min(o.qty, o.display) for o in side_book.get(price, ()))

    def level_qty_true(self, side: int, price: int) -> int:
        """这一档实际有多少股（含没露出来的）。只有引擎和评分能看，界面不能。"""
        side_book = self._bids if side == BUY else self._asks
        return sum(o.qty for o in side_book.get(price, ()))

    def owner_qty(self, side: int, price: int, owner: str) -> int:
        """某人在某一价位上还剩多少股没成交。做市商补量时用。"""
        side_book = self._bids if side == BUY else self._asks
        return sum(o.qty for o in side_book.get(price, ()) if o.owner == owner)

    def queue_ahead(self, oid: int) -> int:
        """我这张单前面还排着多少股——按真实量算，含看不见的部分。

        界面上要把这个数显示给练习者：他看到的盘口只有一百多股，
        实际排在前面的可能是它的十倍，这个落差是必须让他感觉到的。
        """
        o = self._index.get(oid)
        if o is None:
            return 0
        side_book = self._bids if o.side == BUY else self._asks
        ahead = 0
        for other in side_book.get(o.price, ()):
            if other.oid == oid:
                break
            ahead += other.qty
        return ahead

    def orders_of(self, owner: str) -> list[Order]:
        return [self._index[i] for i in self._by_owner.get(owner, ()) if i in self._index]

    # ── 下单 ─────────────────────────────────────────────────────────────
    def limit(self, side: int, price: int, qty: int, owner: str, ts: float,
              ioc: bool = False, display: int | None = None) -> tuple[int | None, list[Trade]]:
        """限价单。能立即成交的先成交，剩下的挂进簿里。

        ioc=True 表示「成交多少算多少，剩下的不挂」。冲着吃单去的策略要用这个：
        否则一张吃不满的单子会把剩余量留在对手价上，凭空把价差压成 1 tick。

        返回 (挂进去的单号或 None, 本次产生的成交)。
        """
        qty = int(qty)
        if qty <= 0:
            return None, []
        # 够不着对手价的单子（绝大多数）直接跳过撮合，省掉一次全簿扫描
        touch = self.best_ask() if side == BUY else self.best_bid()
        crossing = touch is not None and (price >= touch if side == BUY else price <= touch)
        fills = self._match(side, price, qty, owner, ts) if crossing else []
        remaining = qty - sum(f.qty for f in fills)
        if remaining <= 0 or ioc:
            return None, fills
        oid = self._next_oid
        self._next_oid += 1
        order = Order(oid, side, price, remaining, owner, ts,
                      display=remaining if display is None else max(0, int(display)))
        self._insert(order)
        return oid, fills

    def market(self, side: int, qty: int, owner: str, ts: float) -> list[Trade]:
        """市价单：一路吃到底。吃不满就吃多少算多少（簿被吃空的情况）。"""
        limit_price = 10**9 if side == BUY else -(10**9)
        return self._match(side, limit_price, int(qty), owner, ts)

    def cancel(self, oid: int) -> bool:
        o = self._index.pop(oid, None)
        if o is None:
            return False
        self._by_owner.get(o.owner, set()).discard(oid)
        side_book = self._bids if o.side == BUY else self._asks
        q = side_book.get(o.price)
        if q is None:
            return False
        try:
            q.remove(o)
        except ValueError:
            return False
        if not q:
            del side_book[o.price]
            self._touch_removed(o.side, o.price)
        return True

    def cancel_all(self, owner: str) -> int:
        """撤掉某人的全部挂单。一次性做完再统一刷新最优价缓存。"""
        oids = self._by_owner.pop(owner, None)
        if not oids:
            return 0
        n = 0
        for oid in oids:
            o = self._index.pop(oid, None)
            if o is None:
                continue
            side_book = self._bids if o.side == BUY else self._asks
            q = side_book.get(o.price)
            if q is None:
                continue
            try:
                q.remove(o)
            except ValueError:
                continue
            if not q:
                del side_book[o.price]
            n += 1
        self._bb_dirty = self._ba_dirty = True
        return n

    # ── 内部：挂进/摘出 + 最优价缓存 ─────────────────────────────────────
    def _insert(self, order: Order) -> None:
        side_book = self._bids if order.side == BUY else self._asks
        side_book.setdefault(order.price, deque()).append(order)
        self._index[order.oid] = order
        self._by_owner.setdefault(order.owner, set()).add(order.oid)
        if order.side == BUY:
            if not self._bb_dirty and (self._bb is None or order.price > self._bb):
                self._bb = order.price
        else:
            if not self._ba_dirty and (self._ba is None or order.price < self._ba):
                self._ba = order.price

    def _touch_removed(self, side: int, price: int) -> None:
        """某一档被清空了。只有清掉的正好是最优档时才需要重算。"""
        if side == BUY:
            if self._bb == price:
                self._bb_dirty = True
        else:
            if self._ba == price:
                self._ba_dirty = True

    # ── 撮合 ─────────────────────────────────────────────────────────────
    def _match(self, side: int, price: int, qty: int, taker: str, ts: float) -> list[Trade]:
        opposite = self._asks if side == BUY else self._bids
        fills: list[Trade] = []
        left = qty
        while left > 0 and opposite:
            best = self.best_ask() if side == BUY else self.best_bid()
            if best is None:
                break
            # 价格够不着就停
            if (side == BUY and best > price) or (side == SELL and best < price):
                break
            queue = opposite[best]
            while left > 0 and queue:
                resting = queue[0]
                take = min(left, resting.qty)
                resting.qty -= take
                left -= take
                # 成交价永远是挂单方的价——挂单的人先来，他的价说了算
                fills.append(Trade(ts, best, take, taker, resting.owner, side))
                if resting.qty == 0:
                    queue.popleft()
                    self._index.pop(resting.oid, None)
                    self._by_owner.get(resting.owner, set()).discard(resting.oid)
            if not queue:
                del opposite[best]
                self._touch_removed(-side, best)
        self.trades.extend(fills)
        return fills

    # ── 辅助 ─────────────────────────────────────────────────────────────
    def to_price(self, ticks: float) -> float:
        return round(ticks * self.tick, 4)

    def snapshot(self, levels: int = 20) -> dict:
        return {
            "bid": [(self.to_price(p), q) for p, q in self.depth(BUY, levels)],
            "ask": [(self.to_price(p), q) for p, q in self.depth(SELL, levels)],
            "mid": self.to_price(self.mid()) if self.mid() is not None else None,
            "spread_ticks": self.spread_ticks(),
        }

    def recent_trades(self, n: int = 50) -> Iterable[Trade]:
        return self.trades[-n:]
