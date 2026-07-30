"""Core abstractions for all trading strategies.

Design principles
-----------------
* **Low coupling** — strategies never import exchanges, auto_trader, or risk
  manager.  All market data arrives through ``DataContext``.
* **High cohesion** — each strategy is a self-contained computation unit
  responsible only for turning market data into portfolio weights.
* **Pluggable** — the ``StrategyRegistry`` (defined in ``__init__.py``) wires
  concrete subclasses into the engine purely by name, with zero code changes
  in ``auto_trader.py``.

Public types
------------
DataContext        — snapshot of all live data sources passed to every cycle
StrategySignal     — standard output: target weights + diagnostic metadata
BaseStrategy       — abstract base all strategies must implement
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..config import Settings

if TYPE_CHECKING:
    from ..exchanges.base import BaseExchange


# ---------------------------------------------------------------------------
# DataContext  — unified data snapshot passed to every strategy each cycle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DataContext:
    """All available live data sources for one trading cycle.

    Strategies receive a ``DataContext`` instead of a raw exchange so that:

    * Multi-exchange strategies (e.g. Cascade) get both equity *and* crypto.
    * Single-exchange strategies use ``ctx.primary`` and need no changes when
      new exchange types are added.
    * Adding new data sources (alternative data, news feeds, …) only means
      extending this class — no strategy code changes required.

    Attributes
    ----------
    futu:
        Futu OpenD equity exchange, or ``None`` if unavailable / offline.
    crypto:
        CCXT-backed crypto exchange, or ``None`` if offline / unreachable.
    timestamp:
        UTC instant when this cycle started.  Strategies should use this
        value for all ``StrategySignal.timestamp`` fields.
    """

    futu:      "BaseExchange | None"
    crypto:    "BaseExchange | None"
    timestamp: datetime

    @property
    def primary(self) -> "BaseExchange | None":
        """Best available exchange (equity preferred, crypto as fallback)."""
        return self.futu or self.crypto


# ---------------------------------------------------------------------------
# StrategySignal — standard output produced by every strategy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategySignal:
    """Immutable signal output produced by a strategy computation.

    Attributes
    ----------
    strategy_name:
        Short identifier that matches the strategy's ``name`` class attribute
        (e.g. ``"cascade"``, ``"dual_momentum"``).
    timestamp:
        UTC instant when the signal was computed (use ``ctx.timestamp``).
    target_weights:
        ``{symbol: weight}`` mapping.  Values are non-negative and their sum
        is ≤ 1.0.  Symbols follow the exchange convention:
        ``"US.SPY"`` for Futu equities, ``"BTC/USDT"`` for crypto.
    scores:
        Raw numeric scores per symbol *before* normalization (diagnostics
        only — not used for order sizing).
    metadata:
        Strategy-specific audit info.  The following keys are recognised by
        ``RiskManager`` and ``TradingPipeline`` (all optional):

        ``is_full_portfolio`` (bool, default ``False``):
            Set to ``True`` when the strategy produces a *complete* portfolio
            allocation that already incorporates regime detection, asset-class
            budgeting, and volatility targeting (e.g. Cascade).
            ``RiskManager`` will skip the signal-combination / volatility-
            targeting steps and apply *only* hard position caps.

        ``regime`` (str):
            Detected regime label (e.g. ``"BULLISH"``, ``"CRISIS"``).

        ``regime_score`` (float):
            Numerical regime score in [−1, +1].

        ``crypto_data_available`` (bool):
            Whether crypto price data was available during this cycle.

        ``strategy_weight_hint`` (float):
            Preferred combination weight when mixed with other strategies.
            If absent, ``RiskManager`` falls back to config weights.
    """

    strategy_name:  str
    timestamp:      datetime
    target_weights: dict[str, float]
    scores:         dict[str, float]
    metadata:       dict[str, Any]


# ---------------------------------------------------------------------------
# BaseStrategy — abstract interface every strategy must implement
# ---------------------------------------------------------------------------


class BaseStrategy(ABC):
    """Abstract base class for all trading strategies.

    Contract
    --------
    * **Input**: ``DataContext`` — live market data snapshot.
    * **Output**: ``StrategySignal`` — normalized target weights + metadata.

    Rules every concrete subclass must follow
    -----------------------------------------
    1. Define ``name`` as a **non-empty class-level string** (not a method).
       The registry uses this to wire strategies by config name.
    2. Implement ``compute_signal(ctx: DataContext) -> StrategySignal``.
    3. Do **not** submit orders, modify exchange state, or hold cross-cycle
       mutable state — strategies are stateless computational units.
    4. Decorate the class with ``@StrategyRegistry.register(name)`` so the
       engine can discover it without explicit imports.
    """

    #: Short identifier used in config, registry, logs, and StrategySignal.
    #: Must be overridden in every concrete subclass.
    name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Enforce the name requirement at class-definition time.
        # We skip abstract intermediaries (those still have __abstractmethods__).
        if not getattr(cls, "__abstractmethods__", None) and not cls.name:
            raise TypeError(
                f"{cls.__name__} must define `name` as a non-empty class-level "
                "string attribute (e.g.  name = 'my_strategy')."
            )

    def __init__(self, settings: Settings) -> None:
        """Initialise with global configuration."""
        self.settings = settings

    @abstractmethod
    def compute_signal(self, ctx: DataContext) -> StrategySignal:
        """Compute a trading signal from current market data.

        Parameters
        ----------
        ctx:
            DataContext for this cycle.  Use ``ctx.futu``, ``ctx.crypto``,
            and ``ctx.primary`` to access live exchange data.

        Returns
        -------
        StrategySignal
            ``target_weights`` must be normalized so that all values are
            non-negative and their sum ≤ 1.0.

        Raises
        ------
        ValueError
            If insufficient market data is available to produce a signal.
        """
        ...
