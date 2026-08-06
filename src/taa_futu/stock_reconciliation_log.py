"""每天收盘记一行对账快照，把「一共差多少」变成「哪天开始差的」。

为什么要有这个文件
------------------
2026-08-06 查出：账本推算现金比券商少 10,196.67，而 6 只持仓逐只分文不差、
成交按 order_id 差集也一笔没漏。差额横跨 4 个月 5624 笔成交，定位不到是哪天、
哪一类事件造成的。

更早一次（2026-04 重建账本时）遇到过同样的事，当时的处理是把算不平的 14,307
塞进 Epoch 的起点资产——账面立刻就平了，代价是那 14,307 是什么永远查不出来。
Epoch 文件里那句 ``absorbs $14,307 of fee-estimation/dividends vs an assumed
$1M start`` 就是这么来的。把差额塞进期初，等于砸掉体温计让体温显示正常。

这个模块只做观测，不改任何既有数字：每天一行，差额逐日可见。差额哪天跳，
那天只有几十笔成交可查。

一个绕不过去的约束
------------------
富途**模拟盘**禁用了两个接口（2026-08-06 实测）：

- ``order_fee_query``   → 「暂时不支持模拟交易」
- ``get_acc_cash_flow`` → 「模拟账户不支持查询现金流水」

所以真实成交费用在模拟盘上拿不到明细，账本里的费用一直是按固定费率估的
（``fee_source: futu_hk_us_fixed``）。真实盘接上就有真值。

那 10,196.67 到底是什么（2026-08-06 查清，暂不修）
--------------------------------------------------
全部是**手续费估高了**，分红和利息一分没有（模拟盘不派息不计息）。

用券商现金倒推真实费用：起点现金 − 当前现金 − 成交净流出（不含费）
= 1,014,307.31 − 551,724.80 − 447,127.39 = **15,455.12**，而账本记了
25,651.79。5624 笔、1,502,582 股，真实费率约 **0.0103/股**。

拿不同费率配置重算这 5624 笔去凑那个靶子：

===========================================  ==========  ==========
配置                                           重算合计    每股
===========================================  ==========  ==========
当前参数（佣金+平台+结算+TAF，都带最低收费）      22,927.10    0.01526
去掉结算费                                    18,419.35    0.01226
去掉结算费 + 平台费不设最低                     16,711.13    0.01112
去掉结算费 + 佣金/平台费都不设最低               15,000.62    0.00998
===========================================  ==========  ==========

最后一行离靶子只差 454.50（2.9%）。结论：富途美股**模拟盘**实收约
佣金 0.0049/股 + 平台费 0.005/股，**不设每笔最低收费、不收结算费**
（结算费是港股的项目）。当前配置多算的就是这两块。

另外账本里存的 25,651.79 用当前参数重算只有 22,927.10，说明费率参数中途改
过、老成交还留着老参数算出来的数——真要修就得整体重算一遍，改配置不够。

2026-08-06 与账户所有者确认后**暂不修**：差额占总资产 0.98%，优先级低于
其他事。先让这条逐日序列跑起来，之后修完费率，正是靠它验证「差额有没有停止
增长」。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import math
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
RECON_LOG_FILE = REPO_ROOT / "runtime" / "stock_reconciliation_daily.jsonl"

# 美股按美东清算日归集：收盘后跑与次日盘前跑必须落在同一天，否则会出现
# 「一天两行」或「跳过一天」，逐日差分就废了。
CLEARING_TZ = ZoneInfo("America/New_York")

# 差额一天之内跳过这个数就值得看一眼。取值理由：观测到的四个月累计差 10,196.67，
# 摊到约 85 个交易日是每天 120 上下；单日超过 500 说明那天有非常规事件。
DAILY_JUMP_ALERT = 500.0


@dataclass(frozen=True)
class ReconSnapshot:
    """某一个清算日收盘时，券商与账本两边的全部关键数字。"""

    date: str
    recorded_at: str
    broker_total_assets: float
    broker_cash: float
    broker_market_value: float
    book_cash: float
    book_position_cost: float
    cash_gap: float
    total_gap: float
    position_break_count: int
    trade_count: int
    epoch_ts: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _f(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def clearing_date(now_utc: datetime | None = None) -> str:
    now = now_utc or datetime.now(UTC)
    return now.astimezone(CLEARING_TZ).date().isoformat()


def build_snapshot(
    *,
    account: dict[str, Any],
    projection,
    epoch: dict[str, Any],
    reconciliation=None,
    now_utc: datetime | None = None,
) -> ReconSnapshot:
    """把一次对账压成一行。纯函数——不碰网络也不碰磁盘，方便测。

    ``book_cash`` 与 ``reconcile_stock_ledger`` 里 ``expected_cash`` 的算法必须
    一致（起点现金 + 账本现金变动），否则这条序列和界面上的对账结论会打架。
    """
    now = now_utc or datetime.now(UTC)
    snapshot = epoch.get("account_snapshot") if isinstance(epoch, dict) else {}
    start_cash = _f((snapshot or {}).get("cash"))

    broker_total = _f(account.get("total_assets"))
    broker_cash = _f(account.get("cash", account.get("cash_balance")))
    broker_mv = _f(account.get("market_val"))

    book_cash = start_cash + _f(getattr(projection, "cash_delta", 0.0))
    book_cost = sum(
        _f(qty) * _f(getattr(projection, "avg_cost", {}).get(sym))
        for sym, qty in getattr(projection, "positions", {}).items()
    )

    breaks = tuple(getattr(reconciliation, "breaks", ()) or ()) if reconciliation is not None else ()
    position_breaks = sum(1 for b in breaks if str(getattr(b, "kind", "")).startswith("position"))

    return ReconSnapshot(
        date=clearing_date(now),
        recorded_at=now.isoformat(),
        broker_total_assets=round(broker_total, 2),
        broker_cash=round(broker_cash, 2),
        broker_market_value=round(broker_mv, 2),
        book_cash=round(book_cash, 2),
        book_position_cost=round(book_cost, 2),
        # 现金线的差：券商实际现金 - 账本推算现金
        cash_gap=round(broker_cash - book_cash, 2),
        # 总资产线的差。持仓市值两边用同一个（券商的），所以正常情况下
        # total_gap 应当等于 cash_gap；两者一旦分开，说明持仓那条线也出事了。
        total_gap=round(broker_total - (book_cash + broker_mv), 2),
        position_break_count=position_breaks,
        trade_count=int(getattr(projection, "trade_count", 0) or 0),
        epoch_ts=str((epoch or {}).get("ts") or ""),
    )


def load_snapshots(path: Path = RECON_LOG_FILE) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("date"):
            rows.append(payload)
    rows.sort(key=lambda r: str(r.get("date")))
    return rows


def append_snapshot(snapshot: ReconSnapshot, path: Path = RECON_LOG_FILE) -> Path:
    """写入一行。同一清算日重复跑覆盖当天，不追加第二行。

    覆盖而非追加：收盘后跑一次、次日盘前补跑一次是常态，留两行会让逐日差分
    凭空多出一个 0，看上去像那天什么都没发生。
    """
    rows = [r for r in load_snapshots(path) if r.get("date") != snapshot.date]
    rows.append(snapshot.to_dict())
    rows.sort(key=lambda r: str(r.get("date")))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n",
        encoding="utf-8",
    )
    return path


def with_daily_delta(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给每行补上「和上一行比，差额动了多少」。

    这是整个文件存在的理由：累计差额是个没法查的数，逐日增量才指得到具体某天。
    """
    out: list[dict[str, Any]] = []
    prev_cash_gap: float | None = None
    prev_trades: int | None = None
    for row in rows:
        item = dict(row)
        gap = _f(row.get("cash_gap"))
        trades = int(_f(row.get("trade_count")))
        item["cash_gap_delta"] = None if prev_cash_gap is None else round(gap - prev_cash_gap, 2)
        item["trades_today"] = None if prev_trades is None else trades - prev_trades
        item["jump"] = bool(item["cash_gap_delta"] is not None
                            and abs(item["cash_gap_delta"]) > DAILY_JUMP_ALERT)
        out.append(item)
        prev_cash_gap, prev_trades = gap, trades
    return out


def already_recorded_today(path: Path = RECON_LOG_FILE, now_utc: datetime | None = None) -> bool:
    today = clearing_date(now_utc)
    return any(str(row.get("date")) == today for row in load_snapshots(path))


def maybe_record_daily_snapshot(
    *,
    account: dict[str, Any],
    projection,
    epoch: dict[str, Any],
    reconciliation=None,
    path: Path = RECON_LOG_FILE,
    now_utc: datetime | None = None,
) -> ReconSnapshot | None:
    """当天还没记过就记一行，记过就什么都不做。

    挂在股票页上，而不是另起一个常驻定时任务：那个页面本来就已经取到了
    account / projection / reconciliation 三样东西，顺手写一行不用多连一次
    券商、不用装 launchd、不用维护第二个进程。代价是「那天没开过 app 就没有
    那天的数据」——序列会缺日子。逐日差分对缺日子是容忍的（差额照样能定位到
    一个区间），比起为此多养一个常驻进程，这个代价划算。

    真要一天不落，再单独挂 ``taa-futu stock-recon-snapshot`` 到 launchd。
    """
    if already_recorded_today(path, now_utc):
        return None
    snapshot = build_snapshot(
        account=account,
        projection=projection,
        epoch=epoch,
        reconciliation=reconciliation,
        now_utc=now_utc,
    )
    append_snapshot(snapshot, path)
    return snapshot
