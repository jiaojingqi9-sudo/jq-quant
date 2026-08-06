"""每日对账快照。

存在的理由见模块 docstring：2026-08-06 查出现金差 10,196.67，横跨 4 个月
5624 笔成交，无法定位。逐日序列要保证的是「差额哪天动了、动了多少」这件事
不会因为重复跑、跨时区、或者中间断几天而失真。
"""
from datetime import UTC, datetime
from types import SimpleNamespace

from taa_futu.stock_reconciliation_log import (
    already_recorded_today,
    append_snapshot,
    build_snapshot,
    clearing_date,
    load_snapshots,
    maybe_record_daily_snapshot,
    with_daily_delta,
)


def _projection(cash_delta=-472_779.18, positions=None, avg_cost=None, trade_count=5624):
    return SimpleNamespace(
        cash_delta=cash_delta,
        positions=positions or {"US.SPY": 99.0},
        avg_cost=avg_cost or {"US.SPY": 756.575},
        trade_count=trade_count,
    )


def _epoch(cash=1_014_307.31):
    return {"ts": "2026-04-08T00:00:00+00:00",
            "account_snapshot": {"cash": cash, "total_assets": cash, "positions": []}}


def test_snapshot_reproduces_the_observed_gap() -> None:
    """用 2026-08-06 实测到的那组数字，必须还原出 10,196.67。"""
    snap = build_snapshot(
        account={"total_assets": 1_036_743.97, "cash": 551_724.80, "market_val": 485_019.17},
        projection=_projection(),
        epoch=_epoch(),
        now_utc=datetime(2026, 8, 6, 20, 0, tzinfo=UTC),
    )
    assert snap.book_cash == 541_528.13
    assert snap.cash_gap == 10_196.67
    # 持仓市值两边用同一个数，所以总资产的差应当就是现金的差
    assert snap.total_gap == snap.cash_gap


def test_total_gap_separates_from_cash_gap_when_positions_break() -> None:
    """两个 gap 一旦分开，说明持仓那条线也出事了——这正是要能看出来的信号。"""
    snap = build_snapshot(
        account={"total_assets": 1_000_000.0, "cash": 500_000.0, "market_val": 450_000.0},
        projection=_projection(cash_delta=-500_000.0),
        epoch=_epoch(cash=1_000_000.0),
        now_utc=datetime(2026, 8, 6, 20, 0, tzinfo=UTC),
    )
    assert snap.cash_gap == 0.0
    assert snap.total_gap == 50_000.0


def test_clearing_date_uses_new_york_not_utc() -> None:
    """美东 8/6 16:30 收盘后跑，UTC 已经是 8/7。归到 8/6，否则收盘那一跑会
    和次日盘前补跑撞进同一天，或者凭空多出一天。"""
    assert clearing_date(datetime(2026, 8, 6, 20, 30, tzinfo=UTC)) == "2026-08-06"
    assert clearing_date(datetime(2026, 8, 7, 3, 0, tzinfo=UTC)) == "2026-08-06"


def test_same_day_rerun_overwrites_instead_of_appending(tmp_path) -> None:
    """收盘后跑一次、次日盘前补跑一次是常态。留两行会让逐日差分凭空多一个 0，
    看上去像那天什么都没发生。"""
    path = tmp_path / "recon.jsonl"
    for cash in (551_000.0, 551_724.80):
        append_snapshot(
            build_snapshot(
                account={"total_assets": 1_036_743.97, "cash": cash, "market_val": 485_019.17},
                projection=_projection(),
                epoch=_epoch(),
                now_utc=datetime(2026, 8, 6, 20, 0, tzinfo=UTC),
            ),
            path=path,
        )
    rows = load_snapshots(path)
    assert len(rows) == 1
    assert rows[0]["broker_cash"] == 551_724.80      # 保留后写的那次


def test_daily_delta_is_what_makes_the_gap_locatable() -> None:
    rows = [
        {"date": "2026-08-04", "cash_gap": 9_000.0, "trade_count": 5_500},
        {"date": "2026-08-05", "cash_gap": 9_120.0, "trade_count": 5_560},
        {"date": "2026-08-06", "cash_gap": 10_196.67, "trade_count": 5_624},
    ]
    out = with_daily_delta(rows)
    assert out[0]["cash_gap_delta"] is None and out[0]["trades_today"] is None
    assert out[1]["cash_gap_delta"] == 120.0
    assert out[1]["trades_today"] == 60
    assert out[1]["jump"] is False
    assert out[2]["cash_gap_delta"] == 1_076.67
    assert out[2]["jump"] is True          # 单日跳 1000+ 该被标出来


def test_snapshots_stay_sorted_even_if_written_out_of_order(tmp_path) -> None:
    """补记历史某一天之后，序列不能乱——逐日差分依赖顺序。"""
    path = tmp_path / "recon.jsonl"
    for day, cash in ((6, 551_724.80), (4, 550_000.0), (5, 551_000.0)):
        append_snapshot(
            build_snapshot(
                account={"total_assets": 1_036_743.97, "cash": cash, "market_val": 485_019.17},
                projection=_projection(),
                epoch=_epoch(),
                now_utc=datetime(2026, 8, day, 20, 0, tzinfo=UTC),
            ),
            path=path,
        )
    assert [r["date"] for r in load_snapshots(path)] == ["2026-08-04", "2026-08-05", "2026-08-06"]


def test_daily_snapshot_records_once_per_day(tmp_path) -> None:
    """股票页每刷新一次就调一次，一天只能落一行。"""
    path = tmp_path / "recon.jsonl"
    kwargs = dict(
        account={"total_assets": 1_036_743.97, "cash": 551_724.80, "market_val": 485_019.17},
        projection=_projection(),
        epoch=_epoch(),
        path=path,
    )
    first = maybe_record_daily_snapshot(now_utc=datetime(2026, 8, 6, 15, 0, tzinfo=UTC), **kwargs)
    again = maybe_record_daily_snapshot(now_utc=datetime(2026, 8, 6, 19, 0, tzinfo=UTC), **kwargs)
    nextday = maybe_record_daily_snapshot(now_utc=datetime(2026, 8, 7, 15, 0, tzinfo=UTC), **kwargs)

    assert first is not None and again is None and nextday is not None
    assert [r["date"] for r in load_snapshots(path)] == ["2026-08-06", "2026-08-07"]
    assert already_recorded_today(path, datetime(2026, 8, 7, 15, 0, tzinfo=UTC)) is True
    assert already_recorded_today(path, datetime(2026, 8, 8, 15, 0, tzinfo=UTC)) is False
