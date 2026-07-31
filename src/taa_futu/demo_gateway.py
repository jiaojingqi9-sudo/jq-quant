"""演示模式的假富途网关。

设了环境变量 ``JQ_DEMO=1`` 之后，``FutuPaperTrader.__enter__`` 返回本模块的
``DemoTrader``，整个 app 就不再连接 OpenD，改用这里合成的数据。别人 clone 下
仓库、没有富途账号、没有行情数据，也能把六个页面都点开看。

为什么放在 ``__enter__`` 而不是每个调用点：全仓 26 处都写
``with FutuPaperTrader(settings) as trader:``，而 ``with ... as`` 拿到的是
``__enter__`` 的返回值，不是构造出来的对象。所以在那一处返回别的对象，26 个
调用点一行都不用改。``__init__`` 本身不做任何连接，改 ``__enter__`` 也不会影响
测试里 ``FutuPaperTrader.__new__(...)`` 那种绕过构造的用法。

列名从哪来：``demo_data/futu_schema.json``，由 ``futu_watcher/capture_schema.py``
从真实接口抓取，只抓列名和类型、不抓任何数值。手写列名的话，漏一个下游就是
KeyError，而且要到点进那个页面才炸。

数据是合成的，用固定随机种子，所以每次运行结果一样——截图和测试才可复现。
所有数字都刻意取整、取小，让人一眼看出不是真账户。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import random
from typing import Any

import pandas as pd

DEMO_ENV = "JQ_DEMO"
_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "demo_data" / "futu_schema.json"

# 演示用标的：TAA baseline 那五个 ETF，都是公开的宽基代理，不涉及任何个人持仓
DEMO_SYMBOLS = ["US.SPY", "US.EFA", "US.IEF", "US.VNQ", "US.DBC"]
DEMO_NAMES = {
    "US.SPY": "SPDR S&P 500 ETF",
    "US.EFA": "iShares MSCI EAFE ETF",
    "US.IEF": "iShares 7-10Y Treasury",
    "US.VNQ": "Vanguard Real Estate ETF",
    "US.DBC": "Invesco DB Commodity",
}
# 基准价，用来生成看起来合理的行情。数字取整，一看就是演示数据。
DEMO_BASE_PRICE = {"US.SPY": 600.0, "US.EFA": 90.0, "US.IEF": 95.0,
                   "US.VNQ": 95.0, "US.DBC": 25.0}
DEMO_QTY = {"US.SPY": 20, "US.EFA": 30, "US.IEF": 40, "US.VNQ": 25, "US.DBC": 50}

DEMO_ACC_ID = 900000001          # 明显不是真实账户号


def demo_enabled() -> bool:
    return (os.environ.get(DEMO_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def _schema() -> dict[str, Any]:
    try:
        return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    except Exception:
        # схема 文件丢了也不能让演示模式直接崩：退回一份最小列集合，
        # 页面会少几列但仍能渲染。
        return {
            "account_info_fields": ["total_assets", "cash", "market_val", "power",
                                    "unrealized_pl", "realized_pl", "currency"],
            "positions_columns": ["code", "stock_name", "qty", "can_sell_qty",
                                  "cost_price", "market_val", "nominal_price",
                                  "pl_ratio", "pl_val", "currency"],
            "orders_columns": ["code", "stock_name", "trd_side", "order_type",
                               "order_status", "order_id", "qty", "price",
                               "create_time", "updated_time", "dealt_qty",
                               "dealt_avg_price", "currency"],
            "snapshots_columns": ["name", "last_price", "open_price", "high_price",
                                  "low_price", "prev_close_price", "volume",
                                  "turnover", "ask_price", "bid_price",
                                  "ask_vol", "bid_vol", "update_time"],
            "snapshots_index": "code",
            "tickers_columns": ["price", "volume", "ticker_direction"],
        }


def _rng(tag: str) -> random.Random:
    """每类数据一个固定种子——同一次演示里各表互相独立，跨次运行完全可复现。"""
    return random.Random(f"jq-demo::{tag}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _price(code: str, drift: float = 0.0) -> float:
    return round(DEMO_BASE_PRICE.get(code, 100.0) * (1 + drift), 2)


class DemoTrader:
    """鸭子类型兼容 FutuPaperTrader 的只读假网关。

    只实现 app 会调的方法。下单相关的一律拒绝——演示模式绝不能有任何写路径，
    哪怕是写到假账本里，否则截个图都可能让人误以为它真的能交易。
    """

    is_demo = True

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.quote_ctx = None
        self.trade_ctx = None
        self._schema = _schema()

    # ── 上下文管理：什么都不用连，也没什么要关 ──
    def __enter__(self) -> "DemoTrader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    # ── 账户 ──
    def list_accounts(self) -> pd.DataFrame:
        return pd.DataFrame([{"acc_id": DEMO_ACC_ID, "trd_env": "SIMULATE",
                              "trdmarket_auth": ["US"]}])

    def resolve_trade_account(self) -> int:
        return DEMO_ACC_ID

    def resolve_sim_account(self) -> int:
        return DEMO_ACC_ID

    def ensure_trade_unlocked(self) -> None:
        return None

    def healthcheck(self) -> dict:
        return {"ok": True, "ret": 0}

    def get_account_info(self, acc_id: int) -> pd.Series:
        fields = self._schema["account_info_fields"]
        market_val = sum(DEMO_QTY[c] * DEMO_BASE_PRICE[c] for c in DEMO_SYMBOLS)
        cash = 50_000.0
        known = {
            "total_assets": round(cash + market_val, 2),
            "securities_assets": round(market_val, 2),
            "cash": cash,
            "us_cash": cash,
            "market_val": round(market_val, 2),
            "long_mv": round(market_val, 2),
            "short_mv": 0.0,
            "power": cash,
            "net_cash_power": cash,
            "available_funds": cash,
            "avl_withdrawal_cash": cash,
            "max_withdrawal": cash,
            "frozen_cash": 0.0,
            "unrealized_pl": 1_234.56,
            "realized_pl": 567.89,
            "currency": "USD",
            "risk_level": "SAFE",
            "risk_status": "SAFE",
        }
        # schema 里其余字段一律补 0/空，保证下游按名取值不会 KeyError
        data = {f: known.get(f, 0.0 if f not in ("currency", "risk_level", "risk_status",
                                                 "dt_status", "is_pdt") else "") for f in fields}
        return pd.Series(data)

    def get_positions(self, acc_id: int) -> pd.DataFrame:
        cols = self._schema["positions_columns"]
        rng = _rng("positions")
        rows = []
        for code in DEMO_SYMBOLS:
            qty = DEMO_QTY[code]
            cost = DEMO_BASE_PRICE[code]
            last = _price(code, rng.uniform(-0.04, 0.06))
            mv = round(qty * last, 2)
            pl_val = round(qty * (last - cost), 2)
            known = {
                "code": code,
                "stock_name": DEMO_NAMES[code],
                "position_market": "US",
                "qty": float(qty),
                "can_sell_qty": float(qty),
                "cost_price": cost,
                "cost_price_valid": True,
                "average_cost": cost,
                "diluted_cost": cost,
                "market_val": mv,
                "nominal_price": last,
                "pl_ratio": round(pl_val / (qty * cost) * 100, 2),
                "pl_ratio_valid": True,
                "pl_val": pl_val,
                "pl_val_valid": True,
                "position_side": "LONG",
                "unrealized_pl": pl_val,
                "realized_pl": 0.0,
                "currency": "USD",
                "acc_id": DEMO_ACC_ID,
            }
            rows.append({c: known.get(c, 0.0) for c in cols})
        return pd.DataFrame(rows, columns=cols)

    def get_open_orders(self, acc_id: int) -> pd.DataFrame:
        """演示账户不留挂单——空表，但列名齐全。

        列名齐全很重要：下游有 `df["order_status"]` 这种直接取列的写法，
        空 DataFrame 少列一样会 KeyError。
        """
        return pd.DataFrame(columns=self._schema["orders_columns"])

    def get_order_history(self, acc_id: int, start: str, end: str) -> pd.DataFrame:
        cols = self._schema["orders_columns"]
        rng = _rng("orders")
        rows = []
        base = _now() - timedelta(days=20)
        # 每只标的先买后卖，且卖出量不超过已买量。
        # 否则账本会算出「卖了没买过的股票」，日志里刷
        # "SELL unmatched quantity ... excluded from realized P&L"——
        # 演示数据自相矛盾，看的人会以为对账功能坏了。
        held: dict[str, float] = {c: 0.0 for c in DEMO_SYMBOLS}
        for i in range(24):
            code = DEMO_SYMBOLS[i % len(DEMO_SYMBOLS)]
            qty = float(rng.choice([5, 10, 15, 20]))
            side = "SELL" if (i % 3 == 0 and held[code] >= qty) else "BUY"
            if side == "SELL":
                held[code] -= qty
            else:
                held[code] += qty
            px = _price(code, rng.uniform(-0.03, 0.03))
            ts = (base + timedelta(days=i * 0.8, hours=rng.randint(0, 6)))
            stamp = ts.strftime("%Y-%m-%d %H:%M:%S")
            known = {
                "code": code,
                "stock_name": DEMO_NAMES[code],
                "order_market": "US",
                "trd_side": side,
                "order_type": "NORMAL",
                "order_status": "FILLED_ALL",
                "order_id": f"DEMO{1000 + i}",
                "qty": qty,
                "price": px,
                "create_time": stamp,
                "updated_time": stamp,
                "dealt_qty": qty,
                "dealt_avg_price": px,
                "last_err_msg": "",
                "remark": "demo",
                "time_in_force": "DAY",
                "currency": "USD",
                "amount": round(qty * px, 2),
            }
            rows.append({c: known.get(c, "") for c in cols})
        df = pd.DataFrame(rows, columns=cols)
        return df.sort_values("updated_time", ascending=False).reset_index(drop=True)

    # ── 行情 ──
    def get_snapshots(self, symbols: list[str]) -> pd.DataFrame:
        cols = self._schema["snapshots_columns"]
        rng = _rng("snapshots")
        rows = []
        for code in symbols:
            last = _price(code, rng.uniform(-0.03, 0.04))
            prev = _price(code)
            known = {
                "name": DEMO_NAMES.get(code, code.split(".")[-1]),
                "update_time": _now().strftime("%Y-%m-%d %H:%M:%S"),
                "last_price": last,
                "open_price": round(prev * 1.001, 2),
                "high_price": round(last * 1.008, 2),
                "low_price": round(last * 0.992, 2),
                "prev_close_price": prev,
                "volume": rng.randint(2_000_000, 40_000_000),
                "turnover": rng.randint(200_000_000, 4_000_000_000),
                "turnover_rate": round(rng.uniform(0.2, 2.5), 3),
                "suspension": False,
                "lot_size": 1,
                "ask_price": round(last + 0.01, 2),
                "bid_price": round(last - 0.01, 2),
                "ask_vol": rng.randint(100, 3000),
                "bid_vol": rng.randint(100, 3000),
                "amplitude": round(rng.uniform(0.4, 2.2), 2),
                "avg_price": round((last + prev) / 2, 2),
                "volume_ratio": round(rng.uniform(0.6, 1.8), 2),
                "highest52weeks_price": round(prev * 1.18, 2),
                "lowest52weeks_price": round(prev * 0.82, 2),
                "sec_status": "NORMAL",
            }
            rows.append({c: known.get(c, math.nan) for c in cols})
        df = pd.DataFrame(rows, columns=cols)
        df.index = pd.Index(list(symbols), name=self._schema.get("snapshots_index", "code"))
        return df

    def _klines(self, code: str, num: int, minutes: bool) -> pd.DataFrame:
        rng = _rng(f"kline::{code}::{'m' if minutes else 'd'}")
        step = timedelta(minutes=1) if minutes else timedelta(days=1)
        end = _now()
        px = DEMO_BASE_PRICE.get(code, 100.0)
        rows = []
        for i in range(num, 0, -1):
            ts = end - step * i
            px = px * (1 + rng.uniform(-0.008, 0.008))
            o = round(px * (1 + rng.uniform(-0.002, 0.002)), 2)
            c = round(px, 2)
            rows.append({
                "code": code,
                "time_key": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "open": o,
                "close": c,
                "high": round(max(o, c) * 1.003, 2),
                "low": round(min(o, c) * 0.997, 2),
                "volume": rng.randint(10_000, 900_000),
                "turnover": rng.randint(1_000_000, 90_000_000),
            })
        return pd.DataFrame(rows)

    def get_recent_klines(self, code: str, num: int) -> pd.DataFrame:
        return self._klines(code, max(1, num), minutes=True)

    def get_daily_klines(self, code: str, num: int) -> pd.DataFrame:
        return self._klines(code, max(1, num), minutes=False)

    def request_history_klines(self, code: str, start: str, end: str | None = None,
                               **kw) -> pd.DataFrame:
        """按请求的起止日期生成日线，只落在交易日上。

        必须真的覆盖到 start：TAA baseline 要 10 个月均线，也就是至少 11 个
        完整自然月的日线。早先这里固定返回 250 根（约 8 个完整月），股票页就会
        报「Not enough completed monthly bars」——演示模式看起来像坏了。
        """
        rng = _rng(f"hist::{code}")

        def _naive(value, fallback):
            """统一成不带时区的时间戳。

            pd.Timestamp.utcnow() 带时区，而日期字符串解析出来不带，两者直接
            比较会抛 "Cannot compare tz-naive and tz-aware timestamps"。
            """
            if value in (None, ""):
                return fallback
            try:
                ts = pd.Timestamp(value)
            except Exception:
                return fallback
            return ts.tz_localize(None) if ts.tzinfo is not None else ts

        today = pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()
        begin = _naive(start, today - pd.DateOffset(years=3))
        finish = _naive(end, today)
        if finish <= begin:
            finish = begin + pd.DateOffset(years=1)
        # 至少给足三年，免得调用方传了个很近的 start 又要算长周期均线
        if (finish - begin).days < 365 * 3:
            begin = finish - pd.DateOffset(years=3)

        days = pd.bdate_range(begin, finish)          # 只取工作日，近似交易日
        px = DEMO_BASE_PRICE.get(code, 100.0)
        # 从起点往回推，让最新一根落在基准价附近，曲线看起来是"涨上来的"
        px = px / (1.0 + 0.00035 * len(days))
        rows = []
        for ts in days:
            px *= 1 + rng.gauss(0.00035, 0.008)      # 轻微正漂移 + 日波动
            o = round(px * (1 + rng.uniform(-0.002, 0.002)), 2)
            c = round(px, 2)
            rows.append({
                "code": code,
                "time_key": ts.strftime("%Y-%m-%d 00:00:00"),
                "open": o, "close": c,
                "high": round(max(o, c) * 1.004, 2),
                "low": round(min(o, c) * 0.996, 2),
                "volume": rng.randint(500_000, 9_000_000),
                "turnover": rng.randint(50_000_000, 900_000_000),
            })
        return pd.DataFrame(rows)

    def get_recent_tickers(self, code: str, num: int) -> pd.DataFrame:
        rng = _rng(f"ticks::{code}")
        last = DEMO_BASE_PRICE.get(code, 100.0)
        rows = [{
            "price": round(last * (1 + rng.uniform(-0.002, 0.002)), 2),
            "volume": rng.randint(1, 800),
            "ticker_direction": rng.choice(["BUY", "SELL", "NEUTRAL"]),
        } for _ in range(max(1, num))]
        return pd.DataFrame(rows, columns=self._schema["tickers_columns"])

    def get_order_book_safe(self, code: str, depth: int) -> dict[str, Any] | None:
        rng = _rng(f"lob::{code}")
        mid = DEMO_BASE_PRICE.get(code, 100.0)
        bids = [(round(mid - 0.01 * (i + 1), 2), rng.randint(100, 2000), 1)
                for i in range(depth)]
        asks = [(round(mid + 0.01 * (i + 1), 2), rng.randint(100, 2000), 1)
                for i in range(depth)]
        return {"code": code, "Bid": bids, "Ask": asks}

    def subscribe_realtime(self, symbols: list[str]) -> None:
        return None

    def subscribe_types(self, symbols: list[str], types: Any) -> None:
        return None

    def subscribe_push_lob(self, *a, **kw) -> None:
        return None

    # ── 写路径：一律拒绝 ──
    def _refuse(self, what: str):
        raise RuntimeError(
            f"演示模式不允许{what}。这是只读演示（JQ_DEMO=1），"
            "去掉这个环境变量并连上 FutuOpenD 才能做真实操作。")

    def submit_orders(self, *a, **kw):
        self._refuse("下单")

    def cancel_all_open_orders(self, *a, **kw):
        self._refuse("撤单")

    def build_fixed_order(self, *a, **kw):
        self._refuse("构造订单")

    def plan_rebalance(self, *a, **kw):
        self._refuse("生成调仓计划")
