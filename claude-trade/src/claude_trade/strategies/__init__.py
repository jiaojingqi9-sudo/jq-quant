"""Trading strategy package.

Public API
----------
StrategyRegistry    Central registry; use ``@StrategyRegistry.register(name)``
                    to enroll a strategy class.
BaseStrategy        Abstract base class all strategies must implement.
DataContext         Per-cycle data snapshot passed to ``compute_signal()``.
StrategySignal      Standard output type from every strategy.

Adding a new strategy
---------------------
1. Create ``src/claude_trade/strategies/my_strategy.py``.
2. Decorate the class::

       from . import StrategyRegistry

       @StrategyRegistry.register("my_strategy")
       class MyStrategy(BaseStrategy):
           name = "my_strategy"
           ...

3. Import the new module at the bottom of this file (one line)::

       from . import my_strategy  # noqa: F401

4. Add ``"my_strategy"`` to the ``active_strategies`` list in ``.env``.

That's it — no changes to ``auto_trader.py`` or any other engine file.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from .base import BaseStrategy, DataContext, StrategySignal

if TYPE_CHECKING:
    from ..config import Settings

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# StrategyRegistry
# ---------------------------------------------------------------------------


class StrategyRegistry:
    """Central registry for all available trading strategy classes.

    Strategies self-register via the ``@StrategyRegistry.register(name)``
    decorator placed on their class definition.  The engine instantiates
    strategies by name at start-up using ``build()`` or ``build_all()``,
    completely decoupling the engine from any specific strategy implementation.
    """

    _registry: ClassVar[dict[str, type[BaseStrategy]]] = {}

    @classmethod
    def register(cls, name: str):
        """Class decorator that registers a strategy class under *name*.

        Example::

            @StrategyRegistry.register("my_strategy")
            class MyStrategy(BaseStrategy):
                name = "my_strategy"
                ...
        """
        def decorator(strategy_cls: type[BaseStrategy]) -> type[BaseStrategy]:
            if name in cls._registry:
                _log.warning(
                    "StrategyRegistry: overwriting existing entry %r with %s",
                    name, strategy_cls.__name__,
                )
            cls._registry[name] = strategy_cls
            _log.debug("StrategyRegistry: registered %r -> %s", name, strategy_cls.__name__)
            return strategy_cls
        return decorator

    @classmethod
    def build(cls, name: str, settings: "Settings") -> BaseStrategy:
        """Instantiate the strategy registered under *name*.

        Raises
        ------
        KeyError
            If no strategy is registered under *name*.
        """
        if name not in cls._registry:
            available = sorted(cls._registry)
            raise KeyError(
                f"Unknown strategy: {name!r}.  "
                f"Available strategies: {available}"
            )
        strategy = cls._registry[name](settings)
        _log.debug("StrategyRegistry: built %r", name)
        return strategy

    @classmethod
    def build_all(cls, names: list[str], settings: "Settings") -> list[BaseStrategy]:
        """Instantiate all strategies in *names*, preserving order.

        Raises
        ------
        KeyError
            If any name is not registered.
        """
        return [cls.build(n, settings) for n in names]

    @classmethod
    def available(cls) -> list[str]:
        """Return the sorted list of all registered strategy names."""
        return sorted(cls._registry)


# ---------------------------------------------------------------------------
# Eagerly import strategy modules so they self-register via @register.
# To add a new strategy: create its module, then add one import line here.
# ---------------------------------------------------------------------------
from . import cascade             # noqa: E402, F401
from . import dual_momentum       # noqa: E402, F401
from . import rsi_mean_reversion  # noqa: E402, F401
from . import volatility_breakout # noqa: E402, F401


__all__ = [
    "BaseStrategy",
    "DataContext",
    "StrategyRegistry",
    "StrategySignal",
]
