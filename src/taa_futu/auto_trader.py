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


def _strategy_stack_target_weights(
    settings: Settings,
    trader: FutuPaperTrader,
    now_utc: datetime,
    positions: pd.DataFrame,
) -> tuple[dict[str, float], dict[str, object]]:
    baseline_weight, fusion_weight, cascade_weight, _reserve_weight = stack_allocations(settings)
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
        scaled_fusion_weights = {
            code: round(weight * fusion_weight, 6) for code, weight in fusion_plan.target_weights.items()
        }

    scaled_cascade_weights: dict[str, float] = {}
    if cascade_weight > 0:
        cascade_plan = generate_live_cascade_plan(settings, trader)
        diagnostics["cascade_regime"] = cascade_plan.regime_label
        diagnostics["cascade_score"] = round(float(cascade_plan.regime_score), 6)
        diagnostics["cascade_targets"] = tuple(sorted(cascade_plan.target_weights))
        if cascade_plan.note:
            diagnostics["cascade_note"] = cascade_plan.note
        scaled_cascade_weights = {
            code: round(weight * cascade_weight, 6) for code, weight in cascade_plan.target_weights.items()
        }

    return stack_target_weights(baseline_weights, scaled_fusion_weights, scaled_cascade_weights), diagnostics


def _stack_monitoring_detail(settings: Settings, diagnostics: dict[str, object]) -> str:
    detail_parts = [f"stack={stack_label(settings)}"]
    fusion_score = diagnostics.get("fusion_benchmark_score")
    if fusion_score is not None:
        detail_parts.append(f"fusion_benchmark_score={float(fusion_score):.4f}")
    cascade_regime = diagnostics.get("cascade_regime")
    if cascade_regime:
        cascade_score = diagnostics.get("cascade_score")
        if cascade_score is not None:
            detail_parts.append(f"cascade_regime={cascade_regime}({float(cascade_score):+.3f})")
        else:
            detail_parts.append(f"cascade_regime={cascade_regime}")
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
            detail = f"existing_open_orders={len(open_orders)}"
            return "waiting", detail

        positions = trader.get_positions(acc_id)
        ignored_symbols: set[str] = set()

        stack_target_map, diagnostics = _strategy_stack_target_weights(settings, trader, now_utc, positions)

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
