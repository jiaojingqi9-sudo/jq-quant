from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import pandas as pd


class ExchangeError(RuntimeError):
    """Base exception for exchange-related errors."""

    pass


@dataclass(frozen=True)
class PlannedOrder:
    """Represents an order planned by rebalancing logic."""

    symbol: str  # e.g., "US.SPY" for Futu, "BTC/USDT" for crypto
    side: str  # "BUY" or "SELL"
    quantity: float  # Number of shares/units (can be float for crypto, int for stocks)
    limit_price: float  # Price to use for limit order
    reference_price: float  # Current market price (for reference)
    current_qty: float  # Current position quantity
    target_qty: float  # Target position quantity after order
    target_weight: float  # Target portfolio weight


class BaseExchange(ABC):
    """Unified interface for all exchanges (stocks and crypto)."""

    @abstractmethod
    def connect(self) -> None:
        """Establish connection to exchange."""
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """Close connection to exchange."""
        pass

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, *args):
        """Context manager exit."""
        self.disconnect()

    @abstractmethod
    def get_account_value(self) -> float:
        """Get total account value in base currency."""
        pass

    @abstractmethod
    def get_positions(self) -> pd.DataFrame:
        """Get current positions.

        Returns DataFrame with columns:
        - symbol: str (e.g., "US.SPY" or "BTC/USDT")
        - qty: float (quantity held)
        - market_value: float (position value in base currency)
        - avg_cost: float (average cost per unit)
        """
        pass

    @abstractmethod
    def get_price(self, symbol: str) -> float:
        """Get current price for a single symbol."""
        pass

    @abstractmethod
    def get_prices(self, symbols: list[str]) -> dict[str, float]:
        """Get current prices for multiple symbols.

        Returns dict mapping symbol -> price.
        """
        pass

    @abstractmethod
    def get_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        """Get OHLCV (candlestick) data.

        Args:
            symbol: Symbol to fetch (e.g., "US.SPY", "BTC/USDT")
            timeframe: Timeframe (e.g., "1m", "1h", "1d")
            limit: Number of candles to fetch

        Returns DataFrame with columns:
        - timestamp: datetime
        - open: float
        - high: float
        - low: float
        - close: float
        - volume: float
        """
        pass

    @abstractmethod
    def get_order_book(self, symbol: str, depth: int) -> dict | None:
        """Get order book snapshot.

        Returns dict with 'asks' and 'bids' lists of (price, size) tuples,
        or None if unavailable.
        """
        pass

    @abstractmethod
    def submit_order(self, order: PlannedOrder) -> dict:
        """Submit an order to the exchange.

        Args:
            order: PlannedOrder to submit

        Returns dict with keys:
        - order_id: str (exchange-assigned order ID)
        - status: str (e.g., "submitted", "filled", "error")
        - detail: str (order_id or error message)
        """
        pass

    @abstractmethod
    def get_open_orders(self) -> pd.DataFrame:
        """Get all open orders.

        Returns DataFrame with columns:
        - order_id: str
        - symbol: str
        - side: str ("BUY" or "SELL")
        - quantity: float
        - price: float (limit price)
        - status: str
        """
        pass

    @abstractmethod
    def cancel_all_orders(self) -> None:
        """Cancel all open orders."""
        pass

    def plan_rebalance(
        self,
        target_weights: dict[str, float],
        ignore_symbols: set[str] | None = None,
        min_weight_change: float = 0.0,
    ) -> list[PlannedOrder]:
        """Plan a portfolio rebalance from current to target weights.

        Generic rebalancing logic: sell first (to free capital), then buy.

        Args:
            target_weights:    Dict mapping symbol -> target weight (0.0 to 1.0)
            ignore_symbols:    Set of symbols to skip during rebalance
            min_weight_change: Skip orders where |target_weight - current_weight|
                               is below this threshold (e.g. 0.02 = 2%).
                               Set to 0 to disable (default behaviour).

        Returns list of PlannedOrder, sorted SELL-first then BUY.
        """
        ignored = ignore_symbols or set()
        account_value = self.get_account_value()
        if account_value <= 0:
            raise ExchangeError(f"Invalid account value: {account_value}")

        positions = self.get_positions()
        if positions.empty:
            held_symbols = []
        else:
            held_symbols = positions["symbol"].tolist()

        # Build symbol universe: target + current positions
        target_symbols = {sym: w for sym, w in target_weights.items() if sym not in ignored}
        all_symbols = sorted(set(target_symbols.keys()) | set(held_symbols))

        if not all_symbols:
            raise ExchangeError("No symbols to rebalance")

        # Get current snapshot
        prices = self.get_prices(all_symbols)
        position_map = {}
        if not positions.empty:
            for _, row in positions.iterrows():
                sym = row["symbol"]
                if sym not in ignored:
                    position_map[sym] = {"qty": row["qty"], "market_value": row["market_value"]}

        # Plan orders (sell first, then buy)
        planned = []
        for symbol in all_symbols:
            current_qty = position_map.get(symbol, {}).get("qty", 0.0)
            current_value = position_map.get(symbol, {}).get("market_value", 0.0)
            target_weight = target_symbols.get(symbol, 0.0)
            price = prices.get(symbol, 0.0)

            if price <= 0:
                continue

            # ── Minimum-change filter ─────────────────────────────────────────
            # Skip tiny rebalances that wouldn't offset their transaction cost.
            current_weight = current_value / account_value if account_value > 0 else 0.0
            if min_weight_change > 0 and abs(target_weight - current_weight) < min_weight_change:
                continue

            # Calculate target quantity
            target_value = account_value * target_weight
            target_qty = target_value / price

            # Calculate delta
            delta_qty = target_qty - current_qty
            if abs(delta_qty) < 1e-8:
                continue

            side = "BUY" if delta_qty > 0 else "SELL"
            quantity = abs(delta_qty)

            # Respect sell constraints (can't sell more than you have)
            if side == "SELL":
                quantity = min(quantity, current_qty)

            if quantity < 1e-8:
                continue

            planned.append(
                PlannedOrder(
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    limit_price=price,  # Will be adjusted by subclass
                    reference_price=price,
                    current_qty=current_qty,
                    target_qty=target_qty,
                    target_weight=target_weight,
                )
            )

        # Sort: SELL first (to free capital), then BUY
        planned.sort(key=lambda x: (0 if x.side == "SELL" else 1))
        return planned
