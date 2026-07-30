"""Auto-trader engine — lifecycle shell 自动交易引擎

Responsibilities
----------------
This module is intentionally *thin*.  It owns only the machine-level concerns:

* Connect / disconnect exchanges (Futu + CCXT crypto).
* Run the main poll loop (every ``auto_trader_poll_seconds``).
* Delegate *all* trading logic to ``TradingPipeline``.
* Handle SIGINT / SIGTERM for graceful shutdown.
* Recover from consecutive exchange errors via auto-reconnect.
* Write ``runtime/status.json`` after each cycle.
* Run daily log-cleanup via ``market_logger.cleanup_old_logs()``.

What it does NOT do
-------------------
* Know which strategies are active (that's ``StrategyRegistry``).
* Plan or submit orders (that's ``TradingPipeline``).
* Combine signals or apply risk limits (that's ``RiskManager``).

Adding a new strategy
---------------------
1. Create a strategy module in ``strategies/`` and decorate with
   ``@StrategyRegistry.register("my_strategy")``.
2. Set ``ACTIVE_STRATEGIES=my_strategy`` (or add it to the comma-separated
   list) in ``.env``.
3. Done — no changes required here.

Status file layout (runtime/status.json)
{
    "updated_at":           "2026-03-11T14:30:00+00:00",
    "mode":                 "dry_run" | "live",
    "active_strategies":    ["cascade"],
    "regime":               "BULLISH",
    "regime_score":         0.32,
    "target_weights":       {"BTC/USDT": 0.30, "US.SPY": 0.25},
    "total_exposure":       0.55,
    "account_value":        5000.0,
    "cycle_count":          42,
    "error_count":          1,
    "last_error":           "...",
    "last_trade_at":        "2026-03-11T14:00:00+00:00",
    "futu_online":          true,
    "crypto_online":        true,
    "market_hours_open":    true,
    "data_quality":         {"crypto_available": true},
}
"""
from __future__ import annotations

import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Settings
from ..exchanges.base import BaseExchange
from ..risk.manager import RiskManager
from ..strategies import StrategyRegistry
from .pipeline import CycleResult, TradingPipeline, is_equity_market_open
from . import market_logger

logger = logging.getLogger(__name__)

# ── Runtime paths ────────────────────────────────────────────────────────────
_ENGINE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _ENGINE_DIR.parents[2]
_RUNTIME_DIR = _PROJECT_ROOT / "runtime"
_STATUS_FILE  = _RUNTIME_DIR / "status.json"
_HISTORY_FILE = _RUNTIME_DIR / "account_history.jsonl"


def _write_status(data: dict[str, Any]) -> None:
    """Atomically write status.json (never raises)."""
    try:
        _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, default=str, indent=2), encoding="utf-8")
        tmp.replace(_STATUS_FILE)
    except Exception as exc:
        logger.warning("Could not write status.json: %s", exc)


def _append_account_history(account_value: float, regime: str) -> None:
    """Append one row to account_history.jsonl (never raises)."""
    try:
        _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        from datetime import timezone
        row = json.dumps({
            "ts":            datetime.now(timezone.utc).isoformat(),
            "account_value": account_value,
            "regime":        regime,
        })
        with _HISTORY_FILE.open("a", encoding="utf-8") as fh:
            fh.write(row + "\n")
    except Exception as exc:
        logger.warning("Could not append account_history: %s", exc)


def _futu_and_crypto(settings: Settings) -> tuple[BaseExchange | None, BaseExchange | None]:
    """Create exchange instances.  Returns (futu, crypto); either may be None on error."""
    futu: BaseExchange | None = None
    crypto: BaseExchange | None = None

    try:
        from ..exchanges.futu_ex import FutuExchange
        futu = FutuExchange(settings)
        futu.connect()
        logger.info("Futu connected (env=%s)", settings.futu_trd_env)
    except Exception as exc:
        logger.warning("Futu unavailable: %s", exc)
        futu = None

    try:
        from ..exchanges.crypto_ex import CryptoExchange
        crypto = CryptoExchange(settings)
        crypto.connect()
        logger.info("Crypto connected (exchange=%s, sandbox=%s)",
                     settings.crypto_exchange, settings.crypto_sandbox)
    except Exception as exc:
        logger.warning("Crypto unavailable: %s", exc)
        crypto = None

    return futu, crypto


def _primary_exchange(futu: BaseExchange | None, crypto: BaseExchange | None) -> BaseExchange:
    """Return the best available exchange for account-level queries."""
    if futu is not None:
        return futu
    if crypto is not None:
        return crypto
    raise RuntimeError("No exchange available — cannot determine account value.")


# ════════════════════════════════════════════════════════════════════════════
# AutoTrader  (below)  — all cycle logic has moved to TradingPipeline
# ════════════════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════════════════
# AutoTrader  — thin lifecycle shell
# ════════════════════════════════════════════════════════════════════════════

class AutoTrader:
    """Engine lifecycle manager: connect, loop, shutdown, status.

    All trading logic (strategy execution, risk management, order planning,
    and order submission) lives in ``TradingPipeline``.  This class only
    handles the machine-level concerns listed in the module docstring.

    Parameters
    ----------
    settings:
        Loaded Settings object.
    dry_run:
        If ``True``, compute and log orders but do not submit them.
    """

    def __init__(self, settings: Settings, *, dry_run: bool = True) -> None:
        self.settings = settings
        self.dry_run  = dry_run
        self._running = False

        # Counters
        self._cycle_count  = 0
        self._error_count  = 0
        self._last_error:  str = ""
        self._last_trade_ts: datetime | None = None
        self._last_result:   CycleResult | None = None

        # Build the pipeline from config — zero coupling to specific strategies
        available = StrategyRegistry.available()
        missing = [name for name in settings.active_strategies if name not in available]
        if missing:
            configured = ", ".join(settings.active_strategies) or "(empty)"
            missing_text = ", ".join(missing)
            available_text = ", ".join(available) or "(none)"
            raise ValueError(
                "ACTIVE_STRATEGIES contains unknown strategy names. "
                f"Configured: {configured}. Missing: {missing_text}. "
                f"Available strategies: {available_text}. "
                "Add the strategy module import in strategies/__init__.py or fix the .env value."
            )

        strategies  = StrategyRegistry.build_all(settings.active_strategies, settings)
        risk_mgr    = RiskManager(settings)
        self._pipeline  = TradingPipeline(strategies, risk_mgr, settings)

        # Exchanges (connected during run())
        self._futu:   BaseExchange | None = None
        self._crypto: BaseExchange | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def _setup_signal_handlers(self) -> None:
        def _shutdown(signum, frame):
            logger.info("Received signal %s — shutting down …", signum)
            self._running = False

        signal.signal(signal.SIGINT,  _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

    def _connect_exchanges(self) -> None:
        self._futu, self._crypto = _futu_and_crypto(self.settings)
        if self._futu is None and self._crypto is None:
            raise RuntimeError("No exchanges could be connected. Check .env and OpenD.")

    def _disconnect_exchanges(self) -> None:
        for ex in (self._futu, self._crypto):
            if ex is not None:
                try:
                    ex.disconnect()
                except Exception:
                    pass
        self._futu   = None
        self._crypto = None

    # ── Status writing ─────────────────────────────────────────────────────

    def _write_status(self, result: CycleResult | None = None) -> None:
        """Atomically write runtime/status.json with current cycle state."""
        now = datetime.now(timezone.utc)

        # Best-effort account value
        account_value = 0.0
        try:
            primary = self._futu or self._crypto
            if primary is not None:
                account_value = primary.get_account_value()
        except Exception:
            pass

        data: dict[str, Any] = {
            "updated_at":         now.isoformat(),
            "mode":               "dry_run" if self.dry_run else "live",
            "active_strategies":  self.settings.active_strategies,
            "cycle_count":        self._cycle_count,
            "error_count":        self._error_count,
            "last_error":         self._last_error,
            "last_trade_at":      self._last_trade_ts.isoformat() if self._last_trade_ts else None,
            "account_value":      account_value,
            "futu_online":        self._futu is not None,
            "crypto_online":      self._crypto is not None,
            "market_hours_open":  is_equity_market_open(self.settings),
            "data_quality":       {"crypto_available": True},
        }

        if result is not None:
            plan = result.plan
            data["regime"]         = plan.regime
            data["regime_score"]   = plan.metadata.get("regime_score")
            data["target_weights"] = plan.final_weights
            data["total_exposure"] = plan.total_exposure
            data["data_quality"]   = result.data_quality
            data["market_hours_open"] = result.market_open

            # Surface signal metadata (e.g. Cascade's regime_details, budgets)
            for sig in result.signals:
                if sig.metadata.get("is_full_portfolio"):
                    data["regime_details"]       = sig.metadata.get("regime_details", {})
                    data["asset_class_budgets"]  = sig.metadata.get("asset_class_budgets", {})

        _write_status(data)
        _append_account_history(account_value, data.get("regime", "UNKNOWN"))

    # ── Main loop ──────────────────────────────────────────────────────────

    def run(self) -> None:
        """Start the engine loop.  Blocks until stopped."""
        self._setup_signal_handlers()

        mode = "DRY-RUN" if self.dry_run else "LIVE TRADING"
        logger.info(
            "AutoTrader starting [%s] strategies=%s poll=%ds cooldown=%ds",
            mode,
            self.settings.active_strategies,
            self.settings.auto_trader_poll_seconds,
            self.settings.auto_trader_order_cooldown_seconds,
        )

        self._connect_exchanges()
        self._running = True
        try:
            self._loop()
        finally:
            logger.info("Disconnecting exchanges …")
            self._disconnect_exchanges()
            logger.info(
                "AutoTrader stopped. Cycles=%d Errors=%d",
                self._cycle_count, self._error_count,
            )

    def _loop(self) -> None:
        poll     = self.settings.auto_trader_poll_seconds
        cooldown = self.settings.auto_trader_order_cooldown_seconds
        _MAX_CONSEC_ERRORS = 3
        consecutive_errors = 0
        _last_cleanup_date: str = ""

        while self._running:
            cycle_start = time.monotonic()
            self._cycle_count += 1

            # ── Daily log cleanup ─────────────────────────────────────────
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today_str != _last_cleanup_date:
                deleted = market_logger.cleanup_old_logs(
                    keep_days=self.settings.log_retention_days
                )
                if deleted:
                    logger.info("Log cleanup: removed %d old day-director(ies).", deleted)
                _last_cleanup_date = today_str

            # ── Cooldown check ────────────────────────────────────────────
            now = datetime.now(timezone.utc)
            cooldown_ok = True
            if self._last_trade_ts is not None:
                elapsed = (now - self._last_trade_ts).total_seconds()
                if elapsed < cooldown:
                    remaining = int(cooldown - elapsed)
                    logger.info("Cooldown active — %ds remaining, skipping submit.", remaining)
                    cooldown_ok = False

            # ── Run trading pipeline ──────────────────────────────────────
            try:
                result = self._pipeline.run_cycle(
                    futu=self._futu,
                    crypto=self._crypto,
                    dry_run=self.dry_run,
                    cooldown_ok=cooldown_ok,
                )
                self._last_result = result
                if result.submitted:
                    self._last_trade_ts = datetime.now(timezone.utc)

                self._write_status(result)
                consecutive_errors = 0

            except Exception as exc:
                self._error_count  += 1
                consecutive_errors += 1
                self._last_error    = str(exc)
                logger.exception("Cycle %d failed: %s", self._cycle_count, exc)
                market_logger.log_error(f"cycle:{self._cycle_count}", exc)
                self._write_status(result=None)

                if consecutive_errors >= _MAX_CONSEC_ERRORS:
                    logger.warning(
                        "%d consecutive errors — attempting exchange reconnect …",
                        consecutive_errors,
                    )
                    try:
                        self._disconnect_exchanges()
                        self._connect_exchanges()
                        consecutive_errors = 0
                        logger.info("Reconnected successfully.")
                    except Exception as reconnect_exc:
                        logger.error("Reconnect failed: %s", reconnect_exc)

            # ── Sleep until next poll (chunked for responsiveness) ────────
            elapsed = time.monotonic() - cycle_start
            sleep_time = max(0.0, poll - elapsed)
            if sleep_time > 0 and self._running:
                logger.debug("Sleeping %.1fs until next cycle …", sleep_time)
                deadline = time.monotonic() + sleep_time
                while self._running and time.monotonic() < deadline:
                    time.sleep(max(0.0, min(1.0, deadline - time.monotonic())))
