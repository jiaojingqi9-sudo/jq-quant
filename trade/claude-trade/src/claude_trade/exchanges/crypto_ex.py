from __future__ import annotations

from typing import Any

import pandas as pd

from ..config import Settings
from .base import BaseExchange, ExchangeError, PlannedOrder


class CryptoExchange(BaseExchange):
    """Exchange implementation for crypto via ccxt (Binance, OKX, etc.)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.exchange: Any | None = None
        self._exchange_name = settings.crypto_exchange.lower()

    def connect(self) -> None:
        """Establish connection to crypto exchange via ccxt."""
        try:
            import ccxt
        except ImportError:
            raise ExchangeError("ccxt library not installed. Install with: pip install ccxt")

        # Get exchange class
        if self._exchange_name not in ccxt.exchanges:
            raise ExchangeError(f"Exchange {self._exchange_name} not supported by ccxt. Available: {ccxt.exchanges}")

        exchange_class = getattr(ccxt, self._exchange_name)

        # Create exchange instance
        exchange_config = {
            "sandbox": self.settings.crypto_sandbox,
            "enableRateLimit": True,
        }

        # Add credentials if provided
        if self.settings.crypto_api_key:
            exchange_config["apiKey"] = self.settings.crypto_api_key
        if self.settings.crypto_api_secret:
            exchange_config["secret"] = self.settings.crypto_api_secret
        if self.settings.crypto_passphrase:
            exchange_config["password"] = self.settings.crypto_passphrase

        self.exchange = exchange_class(exchange_config)

        # Test connection
        if self.settings.crypto_api_key:
            # Credentials provided — verify they are structurally valid
            try:
                self.exchange.check_required_credentials()
            except Exception as exc:
                raise ExchangeError(f"Failed to authenticate with {self._exchange_name}: {exc}") from exc
        else:
            # No credentials — verify public API connectivity only (OHLCV / prices work without auth)
            try:
                self.exchange.fetch_ticker("BTC/USDT")
            except Exception as exc:
                raise ExchangeError(
                    f"Cannot reach {self._exchange_name} (no API key, public endpoint test failed): {exc}"
                ) from exc

    def disconnect(self) -> None:
        """Close connection to exchange."""
        if self.exchange is not None and hasattr(self.exchange, "close"):
            try:
                self.exchange.close()
            except Exception:
                pass

    def get_account_value(self) -> float:
        """Get total account value in base currency (USDT/USDC)."""
        try:
            balance = self.exchange.fetch_balance()
        except Exception as exc:
            raise ExchangeError(f"Failed to fetch balance: {exc}") from exc

        # Assume 'free' + 'used' under 'total' key gives us the value
        # For crypto, we need to calculate total value in a base currency
        # This is a simplified version - real implementation would convert all to one currency
        if "USDT" in balance:
            return float(balance["USDT"]["total"])
        elif "USDC" in balance:
            return float(balance["USDC"]["total"])
        elif "USD" in balance:
            return float(balance["USD"]["total"])
        else:
            # Fallback: sum all assets by converting to quote currency
            total = 0.0
            for currency, data in balance.items():
                if currency in ["free", "used", "total"]:
                    continue
                total += float(data.get("free", 0)) + float(data.get("used", 0))
            return total

    def get_positions(self) -> pd.DataFrame:
        """Get current positions as DataFrame with symbol, qty, market_value, avg_cost."""
        try:
            balance = self.exchange.fetch_balance()
        except Exception as exc:
            raise ExchangeError(f"Failed to fetch balance: {exc}") from exc

        positions = []
        base_price = self.get_prices(["USDT"])[0] if "USDT" in self.exchange.symbols else 1.0

        for currency, data in balance.items():
            if currency in ["free", "used", "total"]:
                continue

            qty = float(data.get("free", 0)) + float(data.get("used", 0))
            if qty <= 0:
                continue

            # Construct symbol (e.g., "BTC/USDT")
            symbol = f"{currency}/USDT"
            try:
                price = self.get_price(symbol)
                market_value = qty * price
                avg_cost = price  # We don't have cost basis from balance, use current price
            except Exception:
                # If symbol doesn't exist, skip
                continue

            positions.append(
                {
                    "symbol": symbol,
                    "qty": qty,
                    "market_value": market_value,
                    "avg_cost": avg_cost,
                }
            )

        return pd.DataFrame(positions) if positions else pd.DataFrame(columns=["symbol", "qty", "market_value", "avg_cost"])

    def get_price(self, symbol: str) -> float:
        """Get current price for a single symbol."""
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            return float(ticker["last"])
        except Exception as exc:
            raise ExchangeError(f"Failed to fetch price for {symbol}: {exc}") from exc

    def get_prices(self, symbols: list[str]) -> dict[str, float]:
        """Get current prices for multiple symbols."""
        result = {}
        for symbol in symbols:
            try:
                result[symbol] = self.get_price(symbol)
            except ExchangeError:
                pass
        return result

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        """Get OHLCV data.

        Args:
            symbol: Trading pair (e.g., "BTC/USDT")
            timeframe: Timeframe (e.g., "1m", "5m", "1h", "1d")
            limit: Number of candles
        """
        try:
            # Ensure symbol is in lowercase for ccxt
            candles = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        except Exception as exc:
            raise ExchangeError(f"Failed to fetch OHLCV for {symbol}: {exc}") from exc

        # Convert to DataFrame
        df = pd.DataFrame(
            candles,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )

        # Convert timestamp from milliseconds to datetime
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")

        return df[["timestamp", "open", "high", "low", "close", "volume"]]

    def get_order_book(self, symbol: str, depth: int = 10) -> dict | None:
        """Get order book snapshot."""
        try:
            orderbook = self.exchange.fetch_order_book(symbol, limit=depth)
        except Exception:
            return None

        return {
            "asks": orderbook.get("asks", []),
            "bids": orderbook.get("bids", []),
        }

    def submit_order(self, order: PlannedOrder) -> dict:
        """Submit an order to the exchange."""
        try:
            # Get market info to validate order size
            market = self.exchange.market(order.symbol)
            min_amount = market.get("limits", {}).get("amount", {}).get("min", 0)
            min_cost = market.get("limits", {}).get("cost", {}).get("min", 0)

            # Calculate order cost
            order_cost = order.quantity * order.limit_price
            if order_cost < min_cost or order.quantity < min_amount:
                return {
                    "order_id": None,
                    "status": "error",
                    "detail": f"Order size {order.quantity} or cost {order_cost} below minimum",
                }

            # Round quantity to market precision
            precision = market.get("precision", {}).get("amount", 8)
            adjusted_qty = round(order.quantity, precision)

            # Submit limit order
            result = self.exchange.create_limit_order(
                symbol=order.symbol,
                side=order.side.lower(),
                amount=adjusted_qty,
                price=order.limit_price,
            )

            return {
                "order_id": result["id"],
                "status": "submitted",
                "detail": str(result["id"]),
            }
        except Exception as exc:
            return {
                "order_id": None,
                "status": "error",
                "detail": str(exc),
            }

    def get_open_orders(self) -> pd.DataFrame:
        """Get all open orders."""
        try:
            orders = self.exchange.fetch_open_orders()
        except Exception as exc:
            raise ExchangeError(f"Failed to fetch open orders: {exc}") from exc

        if not orders:
            return pd.DataFrame(columns=["order_id", "symbol", "side", "quantity", "price", "status"])

        rows = []
        for order in orders:
            rows.append(
                {
                    "order_id": order["id"],
                    "symbol": order["symbol"],
                    "side": order["side"].upper(),
                    "quantity": float(order["amount"]),
                    "price": float(order["price"]),
                    "status": order.get("status", "unknown").lower(),
                }
            )

        return pd.DataFrame(rows)

    def cancel_all_orders(self) -> None:
        """Cancel all open orders."""
        orders = self.get_open_orders()
        if orders.empty:
            return

        for _, order in orders.iterrows():
            try:
                self.exchange.cancel_order(order["order_id"], symbol=order["symbol"])
            except Exception as exc:
                raise ExchangeError(f"Failed to cancel order {order['order_id']}: {exc}") from exc

    def plan_rebalance(
        self,
        target_weights: dict[str, float],
        ignore_symbols: set[str] | None = None,
    ) -> list[PlannedOrder]:
        """Plan a portfolio rebalance for crypto.

        Crypto doesn't have lot sizes, so we can be more granular with quantities.
        """
        planned = super().plan_rebalance(target_weights, ignore_symbols)

        # For crypto, we can keep the quantities as-is (no lot sizes)
        # But we should validate against market minimums
        adjusted = []
        for order in planned:
            try:
                market = self.exchange.market(order.symbol)
                min_amount = market.get("limits", {}).get("amount", {}).get("min", 0)
                min_cost = market.get("limits", {}).get("cost", {}).get("min", 0)

                # Check if order meets minimum
                order_cost = order.quantity * order.limit_price
                if order_cost >= min_cost and order.quantity >= min_amount:
                    # Round to market precision
                    precision = market.get("precision", {}).get("amount", 8)
                    adjusted_qty = round(order.quantity, precision)

                    adjusted.append(
                        PlannedOrder(
                            symbol=order.symbol,
                            side=order.side,
                            quantity=adjusted_qty,
                            limit_price=order.limit_price,
                            reference_price=order.reference_price,
                            current_qty=order.current_qty,
                            target_qty=order.target_qty,
                            target_weight=order.target_weight,
                        )
                    )
            except Exception:
                # If we can't get market info, skip this order
                pass

        # Re-sort: SELL first
        adjusted.sort(key=lambda x: (0 if x.side == "SELL" else 1))
        return adjusted
