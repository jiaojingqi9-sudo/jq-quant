import pandas as pd

from taa_futu.dashboard_app import (
    _calculate_realized_from_fills,
    _calculate_unrealized_from_positions,
    _clamp_intraday_start,
    _compressed_order_markers,
    _decorate_indicator_columns,
    _epoch_seconds,
    _history_window_from_days,
    _current_strategy_breakdown,
    _live_monitor_run_every,
    _lower_panel_payload,
    _lightweight_chart_html,
    _lob_ladder_view,
    _order_book_view,
    _period_strategy_breakdown,
    _positions_view,
    _visible_order_markers,
)
from taa_futu.config import Settings


def _settings() -> Settings:
    return Settings(
        symbols=("US.SPY", "US.EFA", "US.IEF", "US.VNQ", "US.DBC"),
        benchmark="US.SPY",
        start_date="2025-01-01",
        lookback_months=10,
        signal_timezone="America/New_York",
        fusion_universe=("US.SPY", "US.QQQ", "US.AAPL"),
        fusion_benchmark="US.SPY",
        fusion_lookback_bars=20,
        fusion_opening_range_minutes=15,
        fusion_top_k=2,
        fusion_entry_score=0.35,
        fusion_exit_score=0.20,
        fusion_max_position_weight=0.35,
        fusion_max_gross_exposure=0.90,
        fusion_min_rel_volume=1.10,
        fusion_max_spread_bps=15.0,
        fusion_order_book_depth=3,
        fusion_tick_window=50,
        futu_host="127.0.0.1",
        futu_port=11111,
        futu_trd_market="US",
        futu_trd_env="SIMULATE",
        futu_acc_id=None,
        futu_enable_real_trading=False,
        futu_allow_auto_real=False,
        futu_unlock_trade_password_md5=None,
        futu_price_buffer_bps=10,
        futu_fill_outside_rth=False,
        futu_api_retry_attempts=4,
        futu_api_retry_backoff_seconds=0.0,
        auto_trader_poll_seconds=60,
        auto_trader_market_timezone="America/New_York",
        auto_trader_start_time="09:45",
        auto_trader_end_time="15:55",
        auto_trader_order_cooldown_seconds=300,
        watchdog_min_interval_seconds=240,
        watchdog_max_interval_seconds=540,
        watchdog_outside_window_min_interval_seconds=900,
        watchdog_outside_window_max_interval_seconds=1800,
        watchdog_stale_status_seconds=240,
        watchdog_restart_cooldown_seconds=120,
        stack_baseline_enabled=False,
        stack_baseline_weight=0.0,
        stack_fusion_weight=0.5,
        stack_cascade_weight=0.5,
    )


def test_order_book_view_accepts_four_value_levels() -> None:
    bid, ask = _order_book_view(
        {
            "Bid": [(680.71, 300, 0, {}), (680.70, 900, 0, {})],
            "Ask": [(680.73, 240, 0, {}), (680.74, 1040, 0, {})],
        }
    )

    assert list(bid.columns) == ["价格 / Price", "数量 / Size", "订单数 / Orders"]
    assert list(ask.columns) == ["价格 / Price", "数量 / Size", "订单数 / Orders"]
    assert float(bid.iloc[0]["价格 / Price"]) == 680.71
    assert float(ask.iloc[1]["数量 / Size"]) == 1040


def test_lob_ladder_view_builds_bid_ask_columns() -> None:
    ladder = _lob_ladder_view(
        {
            "Bid": [(680.71, 300, 0, {}), (680.70, 900, 0, {})],
            "Ask": [(680.73, 240, 0, {}), (680.74, 1040, 0, {})],
        },
        2,
    )

    assert list(ladder.columns) == [
        "卖价 / Ask",
        "卖量 / Ask Size",
        "卖单数 / Ask Orders",
        "卖累计 / Ask Cum",
        "档位 / Level",
        "买价 / Bid",
        "买量 / Bid Size",
        "买单数 / Bid Orders",
        "买累计 / Bid Cum",
    ]
    assert float(ladder.iloc[0]["卖价 / Ask"]) == 680.73
    assert float(ladder.iloc[1]["买累计 / Bid Cum"]) == 1200


def test_calculate_realized_from_fills_uses_fifo() -> None:
    order_history = pd.DataFrame(
        [
            {"code": "US.SPY", "trd_side": "BUY", "dealt_qty": 100, "dealt_avg_price": 10.0, "updated_time": "2026-03-10 10:00:00"},
            {"code": "US.SPY", "trd_side": "SELL", "dealt_qty": 40, "dealt_avg_price": 11.5, "updated_time": "2026-03-10 10:30:00"},
            {"code": "US.SPY", "trd_side": "SELL", "dealt_qty": 60, "dealt_avg_price": 12.0, "updated_time": "2026-03-10 11:00:00"},
        ]
    )

    realized = _calculate_realized_from_fills(order_history)

    assert realized == 180.0


def test_calculate_unrealized_from_positions_falls_back_to_pl_val() -> None:
    positions = pd.DataFrame(
        [
            {"code": "US.A", "unrealized_pl": "N/A", "pl_val": 12.5},
            {"code": "US.B", "unrealized_pl": "N/A", "pl_val": -2.0},
        ]
    )

    assert _calculate_unrealized_from_positions(positions) == 10.5


def test_positions_view_uses_pl_val_when_unrealized_is_na() -> None:
    positions = pd.DataFrame(
        [
            {
                "code": "US.A",
                "qty": 10,
                "nominal_price": 100.0,
                "market_val": 1000.0,
                "today_pl_val": "N/A",
                "unrealized_pl": "N/A",
                "realized_pl": "N/A",
                "pl_val": 25.0,
                "pl_ratio": 0.025,
            }
        ]
    )

    frame = _positions_view(positions)

    assert float(frame.iloc[0]["浮动盈亏 / Unrealized"]) == 25.0


def test_history_window_from_days_counts_back_from_end_date() -> None:
    start, end = _history_window_from_days(5, end_on=pd.Timestamp("2026-03-10").date())

    assert str(start) == "2026-03-05"
    assert str(end) == "2026-03-10"


def test_clamp_intraday_start_limits_requested_span() -> None:
    start, clamped = _clamp_intraday_start(
        pd.Timestamp("2026-01-01").date(),
        pd.Timestamp("2026-03-10").date(),
        "K_1M",
    )

    assert clamped is True
    assert str(start) == "2026-02-18"


def test_epoch_seconds_localizes_naive_market_timestamps() -> None:
    seconds = _epoch_seconds(pd.Series(pd.to_datetime(["2026-03-10 09:30:00"])), "US")

    assert seconds == [1773149400]


def test_live_monitor_run_every_respects_toggle_and_minimum() -> None:
    assert _live_monitor_run_every(False, 5) is None
    assert _live_monitor_run_every(True, 1) == "2s"
    assert _live_monitor_run_every(True, 5) == "5s"


def test_lightweight_chart_html_contains_chart_payload() -> None:
    bars = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-03-10 09:30:00", "2026-03-10 09:31:00"]),
            "open": [100.0, 101.0],
            "high": [101.5, 102.0],
            "low": [99.8, 100.8],
            "close": [101.0, 101.8],
            "volume": [1000, 1200],
            "ma5": [None, 100.9],
        }
    )

    html = _lightweight_chart_html(
        bars,
        market="US",
        symbol="US.TEST",
        chart_id="us-test-chart",
        title="测试图",
        subtitle="历史范围",
        overlays=[("MA5", "ma5", "#f6c85f")],
        main_series="line",
    )

    assert '"symbol": "US.TEST"' in html
    assert '"title": "测试图"' in html
    assert '"candles":' in html
    assert '"mainSeries": "line"' in html
    assert '"overlays":' in html
    assert "chart-us-test-chart" in html
    assert "window.LightweightCharts" in html
    assert "kineticScroll" in html
    assert "createSeriesMarkers" in html
    assert "mouseWheel: true" in html
    assert "gesturechange" in html
    assert "setVisibleLogicalRange" in html
    assert "dataZoom" not in html
    assert "lastValueVisible: false" in html


def test_decorate_indicator_columns_adds_macd_rsi_and_kdj() -> None:
    bars = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-03-01", periods=40, freq="D"),
            "open": [100 + idx for idx in range(40)],
            "high": [101 + idx for idx in range(40)],
            "low": [99 + idx for idx in range(40)],
            "close": [100.5 + idx for idx in range(40)],
            "volume": [1000 + idx * 10 for idx in range(40)],
        }
    )

    decorated = _decorate_indicator_columns(bars)

    for column in ["macd_dif", "macd_dea", "macd_hist", "rsi14", "kdj_k", "kdj_d", "kdj_j"]:
        assert column in decorated.columns
    assert decorated["macd_hist"].iloc[-1] == decorated["macd_hist"].iloc[-1]


def test_lower_panel_payload_builds_macd_series() -> None:
    bars = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-03-01", periods=60, freq="D"),
            "open": [100 + idx * 0.2 for idx in range(60)],
            "high": [101 + idx * 0.2 for idx in range(60)],
            "low": [99 + idx * 0.2 for idx in range(60)],
            "close": [100.5 + idx * 0.2 for idx in range(60)],
            "volume": [1000 + idx * 20 for idx in range(60)],
        }
    )
    decorated = _decorate_indicator_columns(bars)

    payload = _lower_panel_payload(decorated, market="US", lower_panel="macd")

    assert payload["key"] == "macd"
    assert payload["histogram"] is not None
    assert len(payload["histogram"]["data"]) == 60
    assert len(payload["lines"]) == 2


def test_current_strategy_breakdown_splits_live_positions_by_current_targets() -> None:
    settings = _settings()
    positions = pd.DataFrame(
        [
            {"code": "US.SPY", "market_val": 1000.0, "unrealized_pl": 100.0},
            {"code": "US.GLD", "market_val": 500.0, "unrealized_pl": 40.0},
        ]
    )

    frame = _current_strategy_breakdown(
        settings=settings,
        positions=positions,
        total_assets=10000.0,
        combined_targets={"US.SPY": 0.50, "US.GLD": 0.10},
        baseline_targets={},
        fusion_targets={"US.SPY": 0.25},
        cascade_targets={"US.SPY": 0.25, "US.GLD": 0.10},
        sleeve_allocations={"baseline": 0.0, "fusion": 0.5, "cascade": 0.5, "reserve": 0.0},
    )

    ours_row = frame.loc[frame["策略组 / Group"] == "我的策略组 / Ours"].iloc[0]
    cascade_row = frame.loc[frame["策略组 / Group"] == "Claude/Cascade"].iloc[0]

    assert float(ours_row["估算持仓市值 / Est. Holdings"]) == 500.0
    assert float(cascade_row["估算持仓市值 / Est. Holdings"]) == 1000.0
    assert float(ours_row["估算浮盈 / Est. Unrealized"]) == 50.0
    assert float(cascade_row["估算浮盈 / Est. Unrealized"]) == 90.0


def test_period_strategy_breakdown_groups_unique_and_shared_symbols() -> None:
    settings = _settings()
    filled = pd.DataFrame(
        [
            {"code": "US.AAPL", "trd_side": "BUY", "dealt_qty": 10, "dealt_avg_price": 100.0, "updated_time": "2026-03-10 10:00:00", "fees_total": 1.0},
            {"code": "US.AAPL", "trd_side": "SELL", "dealt_qty": 10, "dealt_avg_price": 110.0, "updated_time": "2026-03-10 11:00:00", "fees_total": 1.0},
            {"code": "US.GLD", "trd_side": "BUY", "dealt_qty": 5, "dealt_avg_price": 200.0, "updated_time": "2026-03-10 12:00:00", "fees_total": 1.0},
        ]
    )

    frame = _period_strategy_breakdown(filled_cost_view=filled, settings=settings)

    groups = set(frame["策略组 / Group"])
    assert "我的策略组 / Ours" in groups
    assert "Claude/Cascade" in groups or "Shared/Overlap" in groups


def test_visible_order_markers_aligns_intraday_fill_to_previous_bar() -> None:
    markers = pd.DataFrame(
        [
            {"timestamp": "2026-03-10 10:43:02", "price": 100.5, "label": "买点 / Buy", "side": "BUY"},
        ]
    )
    bars = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-03-10 10:42:00", "2026-03-10 10:43:00", "2026-03-10 10:44:00"]),
            "open": [100.0, 100.2, 100.4],
            "high": [100.3, 100.6, 100.7],
            "low": [99.9, 100.1, 100.2],
            "close": [100.2, 100.5, 100.6],
            "volume": [100, 200, 150],
        }
    )

    aligned = _visible_order_markers(markers, bars, align_mode="bar")

    assert str(aligned.iloc[0]["timestamp"]) == "2026-03-10 10:43:00"


def test_visible_order_markers_aligns_daily_fill_to_session_date() -> None:
    markers = pd.DataFrame(
        [
            {"timestamp": "2026-03-10 10:43:02", "price": 100.5, "label": "买点 / Buy", "side": "BUY"},
        ]
    )
    bars = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-03-09 00:00:00", "2026-03-10 00:00:00", "2026-03-11 00:00:00"]),
            "open": [100.0, 100.2, 100.4],
            "high": [100.3, 100.6, 100.7],
            "low": [99.9, 100.1, 100.2],
            "close": [100.2, 100.5, 100.6],
            "volume": [100, 200, 150],
        }
    )

    aligned = _visible_order_markers(markers, bars, align_mode="daily")

    assert str(aligned.iloc[0]["timestamp"]) == "2026-03-10 00:00:00"


def test_compressed_order_markers_combines_same_bar_same_side() -> None:
    markers = pd.DataFrame(
        [
            {"timestamp": "2026-03-10 10:43:02", "price": 100.5, "label": "买点 / Buy", "side": "BUY"},
            {"timestamp": "2026-03-10 10:43:18", "price": 100.6, "label": "买点 / Buy", "side": "BUY"},
            {"timestamp": "2026-03-10 10:43:39", "price": 100.7, "label": "买点 / Buy", "side": "BUY"},
            {"timestamp": "2026-03-10 10:43:40", "price": 100.8, "label": "卖点 / Sell", "side": "SELL"},
        ]
    )
    bars = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-03-10 10:42:00", "2026-03-10 10:43:00", "2026-03-10 10:44:00"]),
            "open": [100.0, 100.2, 100.4],
            "high": [100.3, 100.6, 100.7],
            "low": [99.9, 100.1, 100.2],
            "close": [100.2, 100.5, 100.6],
            "volume": [100, 200, 150],
        }
    )

    compressed = _compressed_order_markers(markers, bars, align_mode="bar")

    assert len(compressed) == 2
    assert int(compressed.iloc[0]["count"]) == 3
    assert str(compressed.iloc[0]["side"]) == "BUY"
    assert int(compressed.iloc[1]["count"]) == 1
    assert str(compressed.iloc[1]["side"]) == "SELL"
