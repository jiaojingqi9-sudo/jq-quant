"""TradingPipeline — decoupled cycle orchestration.

Responsibilities
----------------
* Run all active strategies via their standard ``compute_signal(ctx)``
  interface.
* Combine signals and apply risk management via ``RiskManager``.
* Plan rebalance orders per exchange.
* Gate equity orders by market hours.
* Submit orders (or dry-run log them).
* Return a ``CycleResult`` to the caller (``AutoTrader``).

Design principles
-----------------
* **Zero coupling to specific strategies** — strategies are resolved from
  ``StrategyRegistry`` at construction time; adding or swapping strategies
  requires only a config change.
* **Zero coupling to lifecycle** — this class knows nothing about SIGINT
  handling, reconnect logic, status files, or log rotation.  Those concerns
  live in ``AutoTrader``.
* **Testable** — ``run_cycle()`` takes explicit exchange arguments, so unit
  tests can pass mock exchanges without patching globals.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from zoneinfo import ZoneInfo

from ..config import Settings
from ..exchanges.base import BaseExchange, PlannedOrder
from ..risk.manager import PortfolioPlan, RiskManager
from ..strategies.base import DataContext, StrategySignal
from ..strategies import BaseStrategy
from . import market_logger

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Market-hours helper (lives here, not in auto_trader, to avoid circular imports)
# ---------------------------------------------------------------------------


def is_equity_market_open(settings: Settings) -> bool:
    """Return True if the equity market is currently in regular trading hours.

    Uses NYSE calendar: Mon–Fri, within [equity_open, equity_close) in the
    configured timezone.  Fails-open (returns True) on any parsing error so
    orders are never silently dropped due to a misconfigured timezone.
    """
    try:
        now_et = datetime.now(ZoneInfo(settings.auto_trader_market_timezone))
        if now_et.weekday() >= 5:          # Saturday=5, Sunday=6
            return False
        open_h,  open_m  = map(int, settings.auto_trader_equity_open.split(":"))
        close_h, close_m = map(int, settings.auto_trader_equity_close.split(":"))
        now_minutes   = now_et.hour * 60 + now_et.minute
        open_minutes  = open_h  * 60 + open_m
        close_minutes = close_h * 60 + close_m
        return open_minutes <= now_minutes < close_minutes
    except Exception:
        return True   # fail-open: never silently drop orders


# ---------------------------------------------------------------------------
# CycleResult — output of one complete trading cycle
# ---------------------------------------------------------------------------


@dataclass
class CycleResult:
    """Everything produced by a single ``TradingPipeline.run_cycle()`` call.

    Consumed by ``AutoTrader._write_status()`` and the market logger.
    """

    signals:       list[StrategySignal]   # raw strategy outputs
    plan:          PortfolioPlan          # combined risk-managed plan
    futu_orders:   list[PlannedOrder]     # equity orders (may be [] if market closed)
    crypto_orders: list[PlannedOrder]     # crypto orders (always 24/7)
    market_open:   bool                   # was NYSE open when the cycle ran?
    submitted:     bool                   # were orders actually submitted?
    data_quality:  dict[str, Any]         # {"crypto_available": True, …}

    @property
    def all_orders(self) -> list[PlannedOrder]:
        return self.futu_orders + self.crypto_orders


# ---------------------------------------------------------------------------
# TradingPipeline
# ---------------------------------------------------------------------------


class TradingPipeline:
    """Orchestrates one complete trading cycle end-to-end.

    Parameters
    ----------
    strategies:
        Active strategy instances (created by ``StrategyRegistry.build_all``).
    risk_manager:
        RiskManager instance for signal combination and risk limits.
    settings:
        Global trading configuration.
    """

    def __init__(
        self,
        strategies: list[BaseStrategy],
        risk_manager: RiskManager,
        settings: Settings,
    ) -> None:
        if not strategies:
            raise ValueError("TradingPipeline requires at least one strategy.")
        self._strategies   = strategies
        self._risk         = risk_manager
        self._settings     = settings
        logger.info(
            "TradingPipeline initialised with strategies: %s",
            [s.name for s in strategies],
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run_cycle(
        self,
        futu:    "BaseExchange | None",
        crypto:  "BaseExchange | None",
        dry_run: bool = True,
        cooldown_ok: bool = True,
    ) -> CycleResult:
        """Execute one full trading cycle.

        Parameters
        ----------
        futu:
            Connected Futu OpenD exchange, or ``None`` if offline.
        crypto:
            Connected CCXT crypto exchange, or ``None`` if offline.
        dry_run:
            If ``True``, log orders but do not submit them to the exchange.
        cooldown_ok:
            If ``False``, skip order submission regardless of dry_run.
            The cooldown check lives in ``AutoTrader``; we receive its result
            here so the pipeline stays decoupled from wall-clock concerns.

        Returns
        -------
        CycleResult
            Contains signals, plan, planned orders, and submission outcome.
        """
        now = datetime.now(timezone.utc)
        ctx = DataContext(futu=futu, crypto=crypto, timestamp=now)

        # ── 1. Run all active strategies ──────────────────────────────
        signals = self._run_strategies(ctx)

        # ── 2. Risk management → final portfolio plan ─────────────────
        primary = futu or crypto
        plan = self._risk.build_plan(signals, primary)
        market_logger.log_regime(plan.regime, plan.metadata, ts=now)

        # ── 3. Plan rebalance orders per exchange ─────────────────────
        futu_orders, crypto_orders = self._plan_orders(plan, futu, crypto)

        # ── 4. Market-hours gate (equity only) ────────────────────────
        market_open = is_equity_market_open(self._settings)
        if self._settings.auto_trader_equity_hours_only and not market_open:
            deferred = len(futu_orders)
            if deferred:
                logger.info(
                    "Market closed — deferring %d equity order(s); "
                    "crypto orders (%d) proceed.",
                    deferred, len(crypto_orders),
                )
                futu_orders = []

        # ── 5. Log planned orders ─────────────────────────────────────
        all_orders = futu_orders + crypto_orders
        market_logger.log_orders(all_orders, "planned", ts=now)

        if not all_orders:
            logger.info("No rebalancing needed this cycle.")
            return CycleResult(
                signals=signals, plan=plan,
                futu_orders=[], crypto_orders=[],
                market_open=market_open, submitted=False,
                data_quality=self._extract_data_quality(signals),
            )

        # ── 6. Submit (live mode + cooldown ok) or dry-run log ────────
        submitted = False
        if not dry_run and cooldown_ok:
            self._submit_orders(futu, crypto, futu_orders, crypto_orders, now)
            submitted = True
        elif dry_run:
            logger.info("[dry-run] Would submit %d order(s).", len(all_orders))

        return CycleResult(
            signals=signals, plan=plan,
            futu_orders=futu_orders, crypto_orders=crypto_orders,
            market_open=market_open, submitted=submitted,
            data_quality=self._extract_data_quality(signals),
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _run_strategies(self, ctx: DataContext) -> list[StrategySignal]:
        """Run every active strategy; swallow failures so one bad strategy
        cannot block the cycle."""
        signals: list[StrategySignal] = []
        for strategy in self._strategies:
            try:
                sig = strategy.compute_signal(ctx)
                signals.append(sig)
                market_logger.log_strategy_signal(sig, ts=ctx.timestamp)
                logger.debug(
                    "Strategy %r produced %d weights (total=%.1f%%)",
                    strategy.name,
                    len(sig.target_weights),
                    sum(sig.target_weights.values()) * 100,
                )
            except Exception as exc:
                logger.warning(
                    "Strategy %r raised an exception — skipping: %s",
                    strategy.name, exc,
                )
                market_logger.log_error(f"strategy.{strategy.name}", exc, ts=ctx.timestamp)

        if not signals:
            raise RuntimeError(
                "All active strategies failed to produce a signal this cycle."
            )
        return signals

    def _plan_orders(
        self,
        plan: PortfolioPlan,
        futu:   "BaseExchange | None",
        crypto: "BaseExchange | None",
    ) -> tuple[list[PlannedOrder], list[PlannedOrder]]:
        """Split the plan's final weights by exchange and plan rebalance orders."""
        min_chg = self._settings.rebalance_min_change_pct

        equity_weights = {
            k: v for k, v in plan.final_weights.items() if not ("/" in k)
        }
        crypto_weights = {
            k: v for k, v in plan.final_weights.items() if "/" in k
        }

        futu_orders: list[PlannedOrder] = []
        if futu is not None and equity_weights:
            try:
                futu_orders = futu.plan_rebalance(
                    equity_weights, min_weight_change=min_chg
                )
            except Exception as exc:
                logger.warning("Futu rebalance planning failed: %s", exc)

        crypto_orders: list[PlannedOrder] = []
        if crypto is not None and crypto_weights:
            try:
                crypto_orders = crypto.plan_rebalance(
                    crypto_weights, min_weight_change=min_chg
                )
            except Exception as exc:
                logger.warning("Crypto rebalance planning failed: %s", exc)

        return futu_orders, crypto_orders

    def _submit_orders(
        self,
        futu:          "BaseExchange | None",
        crypto:        "BaseExchange | None",
        futu_orders:   list[PlannedOrder],
        crypto_orders: list[PlannedOrder],
        ts:            datetime,
    ) -> None:
        """Submit planned orders and log results."""
        import pandas as pd

        def _submit(exchange: "BaseExchange", orders: list[PlannedOrder]) -> None:
            results = []
            for order in orders:
                try:
                    result = exchange.submit_order(order)
                    results.append({
                        "symbol": order.symbol,
                        "side":   order.side,
                        "status": result.get("status"),
                        "detail": result.get("detail"),
                    })
                    logger.info(
                        "Order submitted: %s %s %s → %s",
                        order.side, order.quantity, order.symbol,
                        result.get("status"),
                    )
                except Exception as exc:
                    logger.error(
                        "Order submission failed for %s: %s", order.symbol, exc
                    )
                    results.append({
                        "symbol": order.symbol,
                        "side":   order.side,
                        "status": "error",
                        "detail": str(exc),
                    })
            result_df = pd.DataFrame(results) if results else None
            market_logger.log_orders(orders, "submitted", result_df, ts=ts)

        if futu is not None and futu_orders:
            _submit(futu, futu_orders)
        if crypto is not None and crypto_orders:
            _submit(crypto, crypto_orders)

    def _extract_data_quality(self, signals: list[StrategySignal]) -> dict[str, Any]:
        """Pull data-quality flags from strategy metadata."""
        quality: dict[str, Any] = {"crypto_available": True}
        for sig in signals:
            # Any strategy may report crypto availability
            if "crypto_data_available" in sig.metadata:
                quality["crypto_available"] = sig.metadata["crypto_data_available"]
        return quality
