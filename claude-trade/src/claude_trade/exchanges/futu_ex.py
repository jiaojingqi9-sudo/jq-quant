from __future__ import annotations

import logging
import math
import socket
from typing import Any

import pandas as pd

from ..config import Settings
from .base import BaseExchange, ExchangeError, PlannedOrder

logger = logging.getLogger(__name__)


def configure_futu_logging(futu_module) -> None:
    """Configure Futu logging to reduce noise in console."""
    try:
        logger = futu_module.common.ft_logger.logger
    except AttributeError:
        return
    # Keep only real SDK errors on console
    logger.console_level = logging.ERROR


def _price_precision(price_spread: float) -> int:
    """Calculate decimal precision for price based on spread."""
    rendered = f"{price_spread:.8f}".rstrip("0")
    if "." not in rendered:
        return 0
    return len(rendered.split(".", 1)[1])


def _round_limit_price(raw_price: float, price_spread: float, side: str) -> float:
    """Round price to nearest spread for limit orders.

    BUY orders round up (more favorable to seller).
    SELL orders round down (more favorable to buyer).
    """
    spread = price_spread if price_spread and price_spread > 0 else 0.01
    steps = raw_price / spread
    if side == "BUY":
        rounded = math.ceil(steps) * spread
    else:
        rounded = math.floor(steps) * spread
    return round(rounded, _price_precision(spread))


class FutuExchange(BaseExchange):
    """Exchange implementation for Futu OpenD (Hong Kong/US stocks)."""

    TERMINAL_ORDER_STATUSES = {
        "FILLED_ALL",
        "CANCELLED_ALL",
        "CANCELLED_PART",
        "FAILED",
        "DISABLED",
        "DELETED",
    }

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._futu: Any | None = None
        self.quote_ctx = None
        self.trade_ctx = None
        self._acc_id: int | None = None

    def connect(self) -> None:
        """Establish connection to Futu OpenD."""
        try:
            with socket.create_connection((self.settings.futu_host, self.settings.futu_port), timeout=2):
                pass
        except OSError as exc:
            raise ExchangeError(
                f"Cannot connect to Futu OpenD at {self.settings.futu_host}:{self.settings.futu_port}. "
                "Start OpenD and enable OpenAPI first."
            ) from exc

        import futu

        self._futu = futu
        configure_futu_logging(futu)
        self.quote_ctx = futu.OpenQuoteContext(
            host=self.settings.futu_host,
            port=self.settings.futu_port,
        )
        self.trade_ctx = futu.OpenSecTradeContext(
            filter_trdmarket=getattr(futu.TrdMarket, self.settings.futu_trd_market),
            host=self.settings.futu_host,
            port=self.settings.futu_port,
        )

        # Resolve account
        self._acc_id = self._resolve_account()

    def disconnect(self) -> None:
        """Close connection to Futu OpenD."""
        if self.quote_ctx is not None:
            self.quote_ctx.close()
        if self.trade_ctx is not None:
            self.trade_ctx.close()

    def _expect_ok(self, result: tuple) -> Any:
        """Check Futu API result and raise if error."""
        ret, payload = result
        if ret != self._futu.RET_OK:
            raise ExchangeError(str(payload))
        return payload

    def _trade_env(self):
        """Get TrdEnv enum for current trade environment."""
        return getattr(self._futu.TrdEnv, self.settings.futu_trd_env)

    def _trade_market(self):
        """Get TrdMarket enum for current trade market."""
        return getattr(self._futu.TrdMarket, self.settings.futu_trd_market)

    def _trade_env_name(self) -> str:
        """Get trade environment name."""
        return str(self.settings.futu_trd_env).upper()

    def _is_real_env(self) -> bool:
        """Check if trading in real (non-simulated) environment."""
        return self._trade_env_name() == "REAL"

    def _ensure_real_trading_enabled(self) -> None:
        """Verify that real trading is enabled if in REAL environment."""
        if not self._is_real_env():
            return
        if not self.settings.futu_enable_real_trading:
            raise ExchangeError(
                "REAL trading is disabled. Set FUTU_ENABLE_REAL_TRADING=true before submitting live orders."
            )

    def _ensure_trade_unlocked(self) -> None:
        """Unlock trade if in REAL environment."""
        if not self._is_real_env():
            return
        self._ensure_real_trading_enabled()
        if not self.settings.futu_unlock_trade_password_md5:
            raise ExchangeError(
                "REAL trading requires FUTU_UNLOCK_TRADE_PASSWORD_MD5. "
                "Provide the MD5 of your Futu trade password instead of the plain password."
            )
        ret, payload = self.trade_ctx.unlock_trade(password_md5=self.settings.futu_unlock_trade_password_md5)
        if ret != self._futu.RET_OK:
            raise ExchangeError(str(payload))

    def _resolve_account(self) -> int:
        """Find the trading account to use."""
        accounts = self._expect_ok(self.trade_ctx.get_acc_list())
        matching = accounts[accounts["trd_env"] == self._trade_env()]
        matching = matching[
            matching["trdmarket_auth"].apply(
                lambda markets: self.settings.futu_trd_market in markets if isinstance(markets, list) else False
            )
        ]
        if matching.empty:
            raise ExchangeError(
                f"No Futu {self._trade_env_name()} account found in OpenD for market {self.settings.futu_trd_market}."
            )

        if self.settings.futu_acc_id is not None:
            match = matching[matching["acc_id"] == self.settings.futu_acc_id]
            if match.empty:
                raise ExchangeError(f"Configured FUTU_ACC_ID={self.settings.futu_acc_id} not found.")
            return int(match.iloc[0]["acc_id"])

        return int(matching.iloc[0]["acc_id"])

    def _get_currency_for_market(self) -> str:
        """Get base currency for the trading market."""
        currency_by_market = {
            "US": "USD",
            "HK": "HKD",
            "CN": "CNH",
            "SG": "SGD",
            "JP": "JPY",
            "AU": "AUD",
            "CA": "CAD",
            "MY": "MYR",
        }
        return currency_by_market.get(self.settings.futu_trd_market, "USD")

    def get_account_value(self) -> float:
        """Get total account value."""
        self._ensure_trade_unlocked()
        currency = self._get_currency_for_market()
        info = self._expect_ok(
            self.trade_ctx.accinfo_query(
                trd_env=self._trade_env(),
                acc_id=self._acc_id,
                currency=getattr(self._futu.Currency, currency),
            )
        )
        return float(info.iloc[0]["total_assets"])

    def get_positions(self) -> pd.DataFrame:
        """Get current positions as DataFrame with symbol, qty, market_value, avg_cost."""
        self._ensure_trade_unlocked()
        positions = self._expect_ok(
            self.trade_ctx.position_list_query(
                trd_env=self._trade_env(),
                acc_id=self._acc_id,
            )
        )
        if positions.empty:
            return pd.DataFrame(columns=["symbol", "qty", "market_value", "avg_cost"])

        # Filter to current market only
        positions = positions[positions["code"].str.startswith(f"{self.settings.futu_trd_market}.")].copy()
        if positions.empty:
            return pd.DataFrame(columns=["symbol", "qty", "market_value", "avg_cost"])

        # Clean numeric columns
        for column in ["qty", "can_sell_qty", "market_val", "cost_price"]:
            if column in positions.columns:
                positions[column] = pd.to_numeric(positions[column], errors="coerce").fillna(0.0)

        # Keep only active positions (non-zero)
        active_mask = pd.Series(False, index=positions.index)
        for column in ["qty", "can_sell_qty", "market_val"]:
            if column in positions.columns:
                active_mask |= positions[column].ne(0)

        result = positions.loc[active_mask].reset_index(drop=True)

        # Standardize column names
        result = result.rename(
            columns={
                "code": "symbol",
                "qty": "qty",
                "market_val": "market_value",
                "cost_price": "avg_cost",
            }
        )

        return result[["symbol", "qty", "market_value", "avg_cost"]]

    def get_price(self, symbol: str) -> float:
        """Get current price for a single symbol."""
        snapshot = self._expect_ok(self.quote_ctx.get_market_snapshot([symbol]))
        if snapshot.empty:
            raise ExchangeError(f"No price data for {symbol}")
        return float(snapshot.iloc[0]["last_price"])

    def get_prices(self, symbols: list[str]) -> dict[str, float]:
        """Get current prices for multiple symbols."""
        if not symbols:
            return {}
        snapshot = self._expect_ok(self.quote_ctx.get_market_snapshot(symbols))
        result = {}
        for _, row in snapshot.iterrows():
            result[row["code"]] = float(row["last_price"])
        return result

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        """Get OHLCV data.

        For daily bars uses request_history_kl (no subscription needed).
        For intraday uses subscribe + get_cur_kline.

        Args:
            symbol: Stock code (e.g., "US.SPY")
            timeframe: Timeframe - "1d", "1m", "5m", "15m", "30m", "1h"
            limit: Number of candles
        """
        ktype_map = {
            "1m":  "K_1M",
            "5m":  "K_5M",
            "15m": "K_15M",
            "30m": "K_30M",
            "1h":  "K_60M",
            "1d":  "K_DAY",
        }
        ktype_str = ktype_map.get(timeframe, "K_DAY")
        ktype_enum = getattr(self._futu.KLType, ktype_str)

        if timeframe == "1d":
            # ── Daily bars: use request_history_kl (no subscription needed) ──
            import datetime as _dt
            end_date   = _dt.date.today().strftime("%Y-%m-%d")
            start_date = (_dt.date.today() - _dt.timedelta(days=limit * 2)).strftime("%Y-%m-%d")

            ret, data, _ = self.quote_ctx.request_history_kl(
                symbol,
                start=start_date,
                end=end_date,
                ktype=ktype_enum,
                autype=self._futu.AuType.QFQ,
                max_count=limit,
            )
            if ret != self._futu.RET_OK:
                raise ExchangeError(str(data))
        else:
            # ── Intraday bars: subscribe → get_cur_kline → unsubscribe ───────
            sub_type = getattr(self._futu.SubType, ktype_str)
            self.quote_ctx.subscribe([symbol], [sub_type], subscribe_push=False)
            try:
                data = self._expect_ok(
                    self.quote_ctx.get_cur_kline(symbol, limit, ktype_enum, self._futu.AuType.QFQ)
                )
            finally:
                self.quote_ctx.unsubscribe([symbol], [sub_type])

        # Standardise column names
        result = data.copy()
        if "time_key" in result.columns:
            result = result.rename(columns={"time_key": "timestamp"})
        elif "timestamp" not in result.columns and "code_time_key" in result.columns:
            result = result.rename(columns={"code_time_key": "timestamp"})

        if "timestamp" not in result.columns:
            raise ExchangeError(f"OHLCV response missing timestamp column for {symbol}")

        # Fill any missing OHLCV columns with 0 rather than crashing
        for col in ("open", "high", "low", "close", "volume"):
            if col not in result.columns:
                result[col] = 0.0

        result = result.tail(limit).reset_index(drop=True)
        return result[["timestamp", "open", "high", "low", "close", "volume"]]

    def get_order_book(self, symbol: str, depth: int) -> dict | None:
        """Get order book snapshot."""
        ret, payload = self.quote_ctx.get_order_book(symbol, depth)
        if ret != self._futu.RET_OK:
            return None
        return payload

    def submit_order(self, order: PlannedOrder) -> dict:
        """Submit an order to the exchange."""
        self._ensure_real_trading_enabled()
        self._ensure_trade_unlocked()

        # Get snapshot to validate and adjust price if needed
        snapshot = self._expect_ok(self.quote_ctx.get_market_snapshot([order.symbol]))
        snapshot = snapshot.iloc[0]
        lot_size = int(snapshot.get("lot_size", 1)) or 1

        # Respect lot size for Futu
        adjusted_qty = math.floor(order.quantity / lot_size) * lot_size
        if adjusted_qty <= 0:
            return {
                "order_id": None,
                "status": "error",
                "detail": f"Quantity {order.quantity} is less than lot size {lot_size}",
            }

        # Adjust limit price based on current market conditions
        raw_price = float(
            snapshot["ask_price"] if order.side == "BUY" and snapshot["ask_price"] > 0 else snapshot["bid_price"]
        )
        if raw_price <= 0:
            raw_price = order.reference_price

        buffer = self.settings.futu_price_buffer_bps / 10_000
        buffered_price = raw_price * (1 + buffer if order.side == "BUY" else 1 - buffer)
        limit_price = _round_limit_price(
            buffered_price, float(snapshot.get("price_spread", 0.01) or 0.01), order.side
        )

        ret, payload = self.trade_ctx.place_order(
            price=limit_price,
            qty=int(adjusted_qty),
            code=order.symbol,
            trd_side=getattr(self._futu.TrdSide, order.side),
            order_type=self._futu.OrderType.NORMAL,
            trd_env=self._trade_env(),
            acc_id=self._acc_id,
            time_in_force=self._futu.TimeInForce.DAY,
            fill_outside_rth=self.settings.futu_fill_outside_rth,
        )

        if ret == self._futu.RET_OK:
            order_id = payload.iloc[0]["order_id"]
            return {
                "order_id": order_id,
                "status": "submitted",
                "detail": str(order_id),
            }
        else:
            return {
                "order_id": None,
                "status": "error",
                "detail": str(payload),
            }

    def get_open_orders(self) -> pd.DataFrame:
        """Get all open orders."""
        self._ensure_trade_unlocked()
        orders = self._expect_ok(
            self.trade_ctx.order_list_query(
                trd_env=self._trade_env(),
                acc_id=self._acc_id,
                refresh_cache=True,
                order_market=self._trade_market(),
            )
        )
        if orders.empty:
            return pd.DataFrame(columns=["order_id", "symbol", "side", "quantity", "price", "status"])

        # Filter to non-terminal orders
        if "order_status" in orders.columns:
            orders = orders[~orders["order_status"].isin(self.TERMINAL_ORDER_STATUSES)].copy()

        if orders.empty:
            return pd.DataFrame(columns=["order_id", "symbol", "side", "quantity", "price", "status"])

        # Standardize columns
        result = orders.copy()
        result = result.rename(
            columns={
                "code": "symbol",
                "trd_side": "side",
                "qty": "quantity",
                "price": "price",
                "order_status": "status",
            }
        )

        return result[["order_id", "symbol", "side", "quantity", "price", "status"]]

    def cancel_all_orders(self) -> None:
        """Cancel all open orders."""
        self._ensure_trade_unlocked()
        orders = self.get_open_orders()
        if orders.empty:
            return

        for _, order in orders.iterrows():
            ret, payload = self.trade_ctx.cancel_order(
                order_id=order["order_id"],
                trd_env=self._trade_env(),
                acc_id=self._acc_id,
            )
            if ret != self._futu.RET_OK:
                raise ExchangeError(f"Failed to cancel order {order['order_id']}: {payload}")

    def plan_rebalance(
        self,
        target_weights: dict[str, float],
        ignore_symbols: set[str] | None = None,
        min_weight_change: float = 0.0,
    ) -> list[PlannedOrder]:
        """Plan a portfolio rebalance, respecting Futu-specific constraints.

        Adjusts lot sizes and prices according to Futu rules.
        Orders whose weight change is below min_weight_change are skipped.
        """
        # Use base logic to get initial plan (includes min-weight-change filter)
        planned = super().plan_rebalance(target_weights, ignore_symbols, min_weight_change)

        # Nothing to rebalance → return early (avoids get_market_snapshot([]) call)
        if not planned:
            return []

        # Adjust for Futu-specific constraints
        symbols = [p.symbol for p in planned]
        snapshots = self._expect_ok(self.quote_ctx.get_market_snapshot(symbols))
        snapshot_map = snapshots.set_index("code")

        adjusted = []
        for order in planned:
            if order.symbol not in snapshot_map.index:
                logger.warning("plan_rebalance: no snapshot for %s — skipping.", order.symbol)
                continue
            snapshot = snapshot_map.loc[order.symbol]
            lot_size = int(snapshot.get("lot_size", 1)) or 1

            # Respect lot size
            adjusted_qty = math.floor(order.quantity / lot_size) * lot_size
            if adjusted_qty <= 0:
                continue

            # Adjust price
            raw_price = float(
                snapshot["ask_price"] if order.side == "BUY" and snapshot["ask_price"] > 0 else snapshot["bid_price"]
            )
            if raw_price <= 0:
                raw_price = order.reference_price

            buffer = self.settings.futu_price_buffer_bps / 10_000
            buffered_price = raw_price * (1 + buffer if order.side == "BUY" else 1 - buffer)
            limit_price = _round_limit_price(
                buffered_price, float(snapshot.get("price_spread", 0.01) or 0.01), order.side
            )

            adjusted.append(
                PlannedOrder(
                    symbol=order.symbol,
                    side=order.side,
                    quantity=adjusted_qty,
                    limit_price=limit_price,
                    reference_price=order.reference_price,
                    current_qty=order.current_qty,
                    target_qty=order.current_qty + adjusted_qty if order.side == "BUY" else order.current_qty - adjusted_qty,
                    target_weight=order.target_weight,
                )
            )

        # Re-sort: SELL first
        adjusted.sort(key=lambda x: (0 if x.side == "SELL" else 1))
        return adjusted
