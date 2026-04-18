from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime, time as dt_time, timedelta
import json
import os
from pathlib import Path
import signal
import sys
import time
from zoneinfo import ZoneInfo

import pandas as pd

from .config import Settings, load_settings
from .cascade_sleeve import generate_live_cascade_plan
from .fusion_intraday import FusionIntradayStrategy
from .ofim_intraday import OfimIntradayStrategy
from .futu_gateway import FutuPaperTrader, FutuTradeError, FutuTransientError, PlannedOrder
from . import market_logger
from .strategy_stack import (
    effective_fusion_settings,
    fetch_futu_daily_closes,
    scaled_baseline_target_weights,
    stack_allocations,
    stack_label,
    stack_target_weights,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "runtime"
AUTO_TRADER_STATUS_FILE = RUNTIME_DIR / "auto_trader_status.json"
AUTO_TRADER_PID_FILE = RUNTIME_DIR / "auto_trader.pid"
AUTO_TRADER_LOG_FILE = RUNTIME_DIR / "auto_trader.log"


@dataclass
class AutoTraderState:
    last_signature: str = ""
    last_submit_at: datetime | None = None
    # Cascade is a daily strategy — cache its plan so it only recomputes once per
    # trading day instead of every 60-second cycle.  Reusing the plan also prevents
    # intraday oscillation caused by incomplete daily bars returned by Futu K-lines.
    cascade_plan: object = None   # CascadeSleevePlan | None
    cascade_plan_date: str = ""   # "YYYY-MM-DD" in market timezone
    # Track when each symbol was last bought so we can enforce a minimum hold time.
    # This prevents OFIM from entering a position and exiting it 60 seconds later.
    position_entry_times: dict = None  # code → datetime (UTC) of last BUY submission
    # Persistent OFIM instance so prev_order_books is maintained across polling cycles.
    # Kept on state (not as a module global) so AutoTraderState is fully self-contained
    # and multiple concurrent traders or tests don't share state accidentally.
    ofim_strategy: OfimIntradayStrategy | None = None

    def __post_init__(self):
        if self.position_entry_times is None:
            object.__setattr__(self, "position_entry_times", {})


def _is_transient_runtime_error(message: object) -> bool:
    return FutuPaperTrader.is_transient_error(message)


def _log(message: str) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{stamp}] {message}", flush=True)


def _parse_hhmm(raw: str) -> dt_time:
    hour_str, minute_str = raw.split(":", 1)
    return dt_time(hour=int(hour_str), minute=int(minute_str))


def _market_window_state(now_utc: datetime, settings: Settings) -> tuple[bool, str]:
    market_now = _market_now(now_utc, settings)
    if market_now.weekday() >= 5:
        return False, f"weekend ({market_now:%Y-%m-%d %H:%M:%S %Z})"

    start_time = _parse_hhmm(settings.auto_trader_start_time)
    end_time = _parse_hhmm(settings.auto_trader_end_time)
    if start_time <= market_now.time() <= end_time:
        return True, f"inside_window ({market_now:%Y-%m-%d %H:%M:%S %Z})"
    return False, f"outside_window ({market_now:%Y-%m-%d %H:%M:%S %Z})"


def _market_now(now_utc: datetime, settings: Settings) -> datetime:
    return now_utc.astimezone(ZoneInfo(settings.auto_trader_market_timezone))


def _market_day_bounds(now_utc: datetime, settings: Settings) -> tuple[str, str]:
    market_date = _market_now(now_utc, settings).date().isoformat()
    return market_date, market_date


def _filled_orders(order_history: pd.DataFrame) -> pd.DataFrame:
    if order_history.empty:
        return order_history
    rows = order_history.copy()
    rows["dealt_qty_num"] = pd.to_numeric(rows.get("dealt_qty"), errors="coerce").fillna(0.0)
    rows["dealt_price_num"] = pd.to_numeric(rows.get("dealt_avg_price"), errors="coerce").fillna(0.0)
    rows = rows[rows["dealt_qty_num"] > 0].copy()
    if rows.empty:
        return rows
    sort_columns = [column for column in ["updated_time", "create_time"] if column in rows.columns]
    if sort_columns:
        rows = rows.sort_values(sort_columns, ascending=True)
    return rows


def _position_quantity(positions: pd.DataFrame, code: str, column: str = "qty") -> int:
    if positions.empty or column not in positions.columns or "code" not in positions.columns:
        return 0
    rows = positions[positions["code"] == code]
    if rows.empty:
        return 0
    return int(pd.to_numeric(rows[column], errors="coerce").fillna(0.0).sum())


def _order_signature(orders: list[PlannedOrder]) -> str:
    normalized = [f"{order.code}|{order.side}|{order.quantity}|{order.limit_price:.4f}" for order in orders]
    return ";".join(sorted(normalized))


def _oldest_open_order_age(open_orders: pd.DataFrame, now_utc: datetime) -> timedelta | None:
    """Return the age of the oldest open order, or None if timestamps are unavailable.

    Falls back from ``create_time`` to ``updated_time`` if the first column is
    absent or entirely unparseable.  Returns ``None`` only when no usable
    timestamp exists at all — callers treat that as "assume stale".
    """
    for col in ("create_time", "updated_time"):
        if col not in open_orders.columns:
            continue
        try:
            times = pd.to_datetime(open_orders[col], errors="coerce", utc=True)
            oldest = times.dropna().min()
            if not pd.isna(oldest):
                return now_utc - oldest.to_pydatetime()
        except Exception:
            continue
    return None


def _strategy_stack_target_weights(
    settings: Settings,
    trader: FutuPaperTrader,
    now_utc: datetime,
    positions: pd.DataFrame,
    state: "AutoTraderState | None" = None,
) -> tuple[dict[str, float], dict[str, object]]:
    baseline_weight, fusion_weight, ofim_weight, cascade_weight, _reserve_weight = stack_allocations(settings)
    fusion_settings = effective_fusion_settings(settings)

    baseline_weights: dict[str, float] = {}
    diagnostics: dict[str, object] = {}
    if settings.stack_baseline_enabled and baseline_weight > 0:
        baseline_start = max(
            pd.Timestamp(settings.start_date).date(),
            (_market_now(now_utc, settings).date() - timedelta(days=max(730, settings.lookback_months * 45))),
        ).isoformat()
        baseline_prices = fetch_futu_daily_closes(
            trader,
            settings.symbols,
            start=baseline_start,
        )
        baseline_weights = scaled_baseline_target_weights(
            baseline_prices,
            settings,
            reference_date=_market_now(now_utc, settings).date(),
        )

    scaled_fusion_weights: dict[str, float] = {}
    if fusion_weight > 0:
        fusion_positions = positions
        fusion_symbols = set(fusion_settings.fusion_universe)
        if not fusion_positions.empty and fusion_symbols:
            fusion_positions = fusion_positions[fusion_positions["code"].isin(fusion_symbols)].copy()
        held_symbols = set(fusion_positions["code"].tolist()) if not fusion_positions.empty else set()
        fusion_plan = FusionIntradayStrategy(fusion_settings).generate_plan(trader, held_symbols)
        diagnostics["fusion_benchmark_score"] = round(float(fusion_plan.benchmark_score), 6)
        diagnostics["fusion_targets"] = tuple(sorted(fusion_plan.target_weights))
        scaled_fusion_weights = {code: round(weight * fusion_weight, 6) for code, weight in fusion_plan.target_weights.items()}

    scaled_ofim_weights: dict[str, float] = {}
    if ofim_weight > 0 and (fusion_settings.ofim_universe or fusion_settings.ofim_crypto_universe):
        # Build the full set of OFIM-relevant symbols: equity universe + crypto proxies
        # (proxy ETFs are what Futu holds on behalf of crypto positions).
        ofim_equity_symbols = set(fusion_settings.ofim_universe)
        crypto_proxy_map: dict[str, str] = dict(fusion_settings.ofim_crypto_to_proxy or ())
        proxy_etf_symbols = set(crypto_proxy_map.values())
        ofim_trackable_symbols = ofim_equity_symbols | proxy_etf_symbols

        ofim_positions = positions
        if not ofim_positions.empty and ofim_trackable_symbols:
            ofim_positions = ofim_positions[ofim_positions["code"].isin(ofim_trackable_symbols)].copy()

        # held_symbols passed to OFIM: equity codes as-is, but proxy ETF positions are
        # reverse-mapped back to their crypto symbol so OFIM's exit logic triggers correctly.
        proxy_to_crypto: dict[str, str] = {v: k for k, v in crypto_proxy_map.items()}
        raw_held = set(ofim_positions["code"].tolist()) if not ofim_positions.empty else set()
        ofim_held_symbols: set[str] = set()
        for code in raw_held:
            ofim_held_symbols.add(proxy_to_crypto.get(code, code))

        # Reuse the persistent instance so prev_order_books is maintained across cycles.
        # The instance lives on state (not as a module global) so tests and multiple
        # concurrent traders don't accidentally share state.
        if state is None or state.ofim_strategy is None:
            ofim_instance = OfimIntradayStrategy(fusion_settings)
            if state is not None:
                state.ofim_strategy = ofim_instance
        else:
            ofim_instance = state.ofim_strategy
            # Settings may change between cycles (e.g. after a weight edit) — update them
            object.__setattr__(ofim_instance, "settings", fusion_settings)
        ofim_plan = ofim_instance.generate_plan(trader, ofim_held_symbols)
        diagnostics["ofim_benchmark_score"] = round(float(ofim_plan.benchmark_score), 6)
        diagnostics["ofim_targets"] = tuple(sorted(ofim_plan.target_weights))
        diagnostics["ofim_top"] = tuple(sorted(ofim_plan.target_weights, key=ofim_plan.target_weights.get, reverse=True)[:3])
        scaled_ofim_weights = {code: round(weight * ofim_weight, 6) for code, weight in ofim_plan.target_weights.items()}

    scaled_cascade_weights: dict[str, float] = {}
    if cascade_weight > 0:
        market_date = _market_now(now_utc, settings).date().isoformat()
        # Daily caching: Cascade is a daily strategy — reuse the same plan all day.
        # This prevents intraday churn from incomplete K-line bars re-running every 60 s.
        if (
            state is not None
            and state.cascade_plan is not None
            and state.cascade_plan_date == market_date
        ):
            cascade_plan = state.cascade_plan
            diagnostics["cascade_cached"] = True
        else:
            cascade_plan = generate_live_cascade_plan(settings, trader)
            if state is not None:
                state.cascade_plan = cascade_plan
                state.cascade_plan_date = market_date
            _log(
                f"cascade: generated new daily plan "
                f"(date={market_date}, regime={cascade_plan.regime_label}, "
                f"score={cascade_plan.regime_score:+.3f})"
            )
        diagnostics["cascade_regime"] = cascade_plan.regime_label
        diagnostics["cascade_score"] = round(float(cascade_plan.regime_score), 6)
        diagnostics["cascade_targets"] = tuple(sorted(cascade_plan.target_weights))
        if cascade_plan.note:
            diagnostics["cascade_note"] = cascade_plan.note
        scaled_cascade_weights = {
            code: round(weight * cascade_weight, 6) for code, weight in cascade_plan.target_weights.items()
        }

    return stack_target_weights(baseline_weights, scaled_fusion_weights, scaled_ofim_weights, scaled_cascade_weights), diagnostics


def _stack_monitoring_detail(settings: Settings, diagnostics: dict[str, object]) -> str:
    detail_parts = [f"stack={stack_label(settings)}"]
    fusion_score = diagnostics.get("fusion_benchmark_score")
    if fusion_score is not None:
        detail_parts.append(f"fusion_bm={float(fusion_score):.4f}")
    ofim_score = diagnostics.get("ofim_benchmark_score")
    if ofim_score is not None:
        detail_parts.append(f"ofim_bm={float(ofim_score):.4f}")
    ofim_top = diagnostics.get("ofim_top")
    if ofim_top:
        detail_parts.append(f"ofim_top={','.join(str(s).replace('US.', '') for s in ofim_top)}")
    cascade_regime = diagnostics.get("cascade_regime")
    if cascade_regime:
        cascade_score = diagnostics.get("cascade_score")
        if cascade_score is not None:
            detail_parts.append(f"cascade_regime={cascade_regime}({float(cascade_score):+.3f})")
        else:
            detail_parts.append(f"cascade_regime={cascade_regime}")
    cascade_targets = diagnostics.get("cascade_targets")
    if cascade_targets is not None:
        tgt_str = ",".join(str(s).replace("US.", "") for s in cascade_targets) if cascade_targets else "none"
        detail_parts.append(f"cascade_targets={tgt_str}")
    cascade_note = diagnostics.get("cascade_note")
    if cascade_note:
        detail_parts.append(str(cascade_note))
    return " | ".join(detail_parts)


def _stack_runtime_detail(settings: Settings, market_detail: str) -> str:
    return f"stack={stack_label(settings)} | {market_detail}"


def _write_status(
    *,
    running: bool,
    action: str,
    detail: str,
    market_open: bool,
    settings: Settings,
    state: AutoTraderState,
) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "running": running,
        "pid": os.getpid(),
        "updated_at": datetime.now(UTC).isoformat(),
        "action": action,
        "detail": detail,
        "market_open": market_open,
        "poll_seconds": settings.auto_trader_poll_seconds,
        "timezone": settings.auto_trader_market_timezone,
        "window_start": settings.auto_trader_start_time,
        "window_end": settings.auto_trader_end_time,
        "last_signature": state.last_signature,
        "last_submit_at": state.last_submit_at.isoformat() if state.last_submit_at else None,
        "log_file": str(AUTO_TRADER_LOG_FILE),
    }
    AUTO_TRADER_STATUS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _register_pid() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if AUTO_TRADER_PID_FILE.exists():
        try:
            current_pid = int(AUTO_TRADER_PID_FILE.read_text(encoding="utf-8").strip())
        except ValueError:
            current_pid = 0
        if current_pid and _is_pid_running(current_pid):
            raise SystemExit(f"Auto trader is already running with pid {current_pid}.")
    AUTO_TRADER_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def validate_auto_trader_mode(settings: Settings, *, submit: bool) -> None:
    stack_allocations(settings)
    if settings.futu_trd_env == "SIMULATE" or not submit:
        return
    if not settings.futu_enable_real_trading:
        raise SystemExit("REAL trading is disabled. Set FUTU_ENABLE_REAL_TRADING=true first.")
    if not settings.futu_allow_auto_real:
        raise SystemExit("REAL auto trading is locked. Set FUTU_ALLOW_AUTO_REAL=true only after manual verification.")
    if not settings.futu_unlock_trade_password_md5:
        raise SystemExit("REAL auto trading requires FUTU_UNLOCK_TRADE_PASSWORD_MD5.")


def _cleanup_files() -> None:
    if AUTO_TRADER_PID_FILE.exists():
        AUTO_TRADER_PID_FILE.unlink()


def run_cycle(settings: Settings, state: AutoTraderState, *, submit: bool) -> tuple[str, str]:
    now_utc = datetime.now(UTC)
    market_open, window_detail = _market_window_state(now_utc, settings)
    if not market_open:
        return "waiting", _stack_runtime_detail(settings, window_detail)

    with FutuPaperTrader(settings) as trader:
        acc_id = trader.resolve_trade_account()
        open_orders = trader.get_open_orders(acc_id)
        if not open_orders.empty:
            stale_threshold = timedelta(minutes=settings.auto_trader_order_stale_minutes)
            oldest_age = _oldest_open_order_age(open_orders, now_utc)
            # Treat unreadable timestamps as stale so we never get stuck permanently.
            is_stale = oldest_age is None or oldest_age >= stale_threshold
            if is_stale:
                age_desc = f"{oldest_age.total_seconds():.0f}s" if oldest_age else "unknown"
                n_cancelled = trader.cancel_all_open_orders(acc_id)
                _log(
                    f"auto-cancelled {n_cancelled} stale open order(s) "
                    f"(oldest_age={age_desc}, threshold={settings.auto_trader_order_stale_minutes}min) "
                    "— proceeding with fresh cycle"
                )
                # Fall through: recompute targets with fresh prices immediately.
            else:
                age_s = int(oldest_age.total_seconds())
                threshold_s = int(stale_threshold.total_seconds())
                return "waiting", (
                    f"waiting_for_fill: open_orders={len(open_orders)} "
                    f"oldest={age_s}s/{threshold_s}s"
                )

        positions = trader.get_positions(acc_id)
        ignored_symbols: set[str] = set()

        stack_target_map, diagnostics = _strategy_stack_target_weights(settings, trader, now_utc, positions, state)

        # Minimum hold time: prevent exiting a recently entered position.
        # If a BUY was submitted within AUTO_TRADER_MIN_HOLD_MINUTES minutes,
        # add that symbol to ignored_symbols so plan_rebalance cannot generate
        # a SELL for it. Full exits (target_weight already 0 AND no held position)
        # are unaffected because ignored_symbols only skips sell-side rebalancing.
        min_hold = timedelta(minutes=settings.auto_trader_min_hold_minutes)
        if min_hold.total_seconds() > 0 and state is not None:
            for code, entry_time in list(state.position_entry_times.items()):
                age = now_utc - entry_time
                # Always clean up symbols we no longer hold, regardless of hold-time window.
                # This covers the case where a position was closed via a SELL order that
                # didn't go through our SELL path (e.g. manual close or broker cancel).
                if _position_quantity(positions, code) == 0:
                    state.position_entry_times.pop(code, None)
                    continue
                if age < min_hold:
                    # Still within hold window and position is live — protect from exit
                    if code not in stack_target_map:
                        ignored_symbols.add(code)
                        remaining_s = int((min_hold - age).total_seconds())
                        _log(
                            f"hold-protect: keeping {code} position "
                            f"(entered {int(age.total_seconds())}s ago, "
                            f"hold={settings.auto_trader_min_hold_minutes}min, "
                            f"exits in {remaining_s}s)"
                        )
                else:
                    # Hold window expired — clean up so the dict doesn't grow unbounded
                    state.position_entry_times.pop(code, None)

        planned_orders: list[PlannedOrder] = []
        strategy_orders: list[PlannedOrder] = []
        held_symbols = set()
        if not positions.empty:
            held_symbols = set(positions.loc[~positions["code"].isin(ignored_symbols), "code"].tolist())
        if stack_target_map or held_symbols:
            _account, strategy_orders = trader.plan_rebalance(stack_target_map, ignore_symbols=ignored_symbols)
            planned_orders.extend(strategy_orders)

        if not planned_orders:
            detail_text = _stack_monitoring_detail(settings, diagnostics)
            if not stack_target_map and not held_symbols:
                return ("monitoring", f"no_entry_signal {detail_text}")
            return "monitoring", f"no_rebalance_needed {detail_text}"

        signature = _order_signature(planned_orders)
        if (
            signature == state.last_signature
            and state.last_submit_at is not None
            and (now_utc - state.last_submit_at).total_seconds() < settings.auto_trader_order_cooldown_seconds
        ):
            return "cooldown", f"duplicate_plan_within_{settings.auto_trader_order_cooldown_seconds}s"

        # Log the planned orders before any submission decision (下单决定落盘)
        market_logger.log_orders(planned_orders, "planned", ts=now_utc)

        if not submit:
            return (
                "planned",
                f"stack={stack_label(settings)} strategy_orders={len(strategy_orders)} "
                f"stack_symbols={len(stack_target_map)} signature={signature}",
            )

        result = trader.submit_orders(planned_orders)
        # Log submission outcome (whether each order was accepted or errored)
        market_logger.log_orders(planned_orders, "submitted", result_df=result, ts=now_utc)
        submitted = int((result["status"] == "submitted").sum()) if not result.empty else 0
        errored = int((result["status"] == "error").sum()) if not result.empty else 0
        state.last_signature = signature
        state.last_submit_at = now_utc
        # Record entry times for BUY orders so the hold-time guard can protect them.
        # Clear entry times for SELL orders (position closed, protection no longer needed).
        for order in planned_orders:
            if order.side == "BUY":
                state.position_entry_times[order.code] = now_utc
            elif order.side == "SELL":
                state.position_entry_times.pop(order.code, None)
        action_name = "submitted_with_errors" if errored else "submitted"
        transient_only = False
        if errored:
            error_rows = result.loc[result["status"] == "error", "detail"].astype(str)
            transient_only = bool(len(error_rows)) and error_rows.map(_is_transient_runtime_error).all()
        if submitted == 0 and errored > 0:
            action_name = "transient_error" if transient_only else "error"
        elif errored and transient_only:
            action_name = "submitted_with_transient_errors"
        error_detail = ""
        if errored:
            last_error = str(result.loc[result["status"] == "error", "detail"].iloc[-1])
            error_detail = f" submit_errors={errored} last_error={last_error}"
        return (
            action_name,
            f"stack={stack_label(settings)} submitted_orders={submitted} strategy_orders={len(strategy_orders)} "
            f"stack_symbols={len(stack_target_map)} signature={signature}{error_detail}",
        )


def run_auto_trader(settings: Settings, *, submit: bool) -> None:
    validate_auto_trader_mode(settings, submit=submit)

    stop_requested = False
    state = AutoTraderState()

    def _handle_signal(signum, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True
        _log(f"received signal {signum}, shutting down")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    _register_pid()
    _write_status(
        running=True,
        action="starting",
        detail="auto trader booting",
        market_open=False,
        settings=settings,
        state=state,
    )
    _log("auto trader started")

    try:
        while not stop_requested:
            market_open, window_detail = _market_window_state(datetime.now(UTC), settings)
            _write_status(
                running=True,
                action="polling",
                detail=_stack_runtime_detail(settings, window_detail),
                market_open=market_open,
                settings=settings,
                state=state,
            )
            try:
                action, detail = run_cycle(settings, state, submit=submit)
                _log(f"{action}: {detail}")
            except FutuTransientError as exc:
                action = "transient_error"
                detail = str(exc)
                market_logger.log_error("auto_trader_transient_error", exc)
                _log(f"transient_error: {detail}")
            except FutuTradeError as exc:
                detail = str(exc)
                if _is_transient_runtime_error(detail):
                    action = "transient_error"
                    market_logger.log_error("auto_trader_transient_error", exc)
                    _log(f"transient_error: {detail}")
                else:
                    action = "error"
                    market_logger.log_error("auto_trader_error", exc)
                    _log(f"error: {detail}")
            except Exception as exc:  # pragma: no cover - safety net for daemon process
                detail = f"{type(exc).__name__}: {exc}"
                if _is_transient_runtime_error(detail):
                    action = "transient_error"
                    market_logger.log_error("auto_trader_transient_error", exc)
                    _log(f"transient_error: {detail}")
                else:
                    action = "error"
                    market_logger.log_error("auto_trader_exception", exc)
                    _log(f"error: {detail}")

            _write_status(
                running=True,
                action=action,
                detail=(
                    detail
                    if action != "waiting"
                    else _stack_runtime_detail(settings, window_detail)
                    if detail.startswith(("stack=", "weekend", "outside_window"))
                    else detail
                ),
                market_open=market_open,
                settings=settings,
                state=state,
            )

            sleep_until = time.time() + settings.auto_trader_poll_seconds
            while not stop_requested and time.time() < sleep_until:
                time.sleep(1)
    finally:
        _write_status(
            running=False,
            action="stopped",
            detail="auto trader stopped",
            market_open=False,
            settings=settings,
            state=state,
        )
        _cleanup_files()
        _log("auto trader stopped")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Fusion intraday strategy in a continuous loop.")
    parser.add_argument("--dry-run", action="store_true", help="Monitor continuously but do not submit orders.")
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    settings = load_settings()
    run_auto_trader(settings, submit=not args.dry_run)


if __name__ == "__main__":
    main()
