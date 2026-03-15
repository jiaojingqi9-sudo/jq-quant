from __future__ import annotations

from dataclasses import dataclass
import math
import socket
import time
from typing import Any

import pandas as pd

from .config import Settings
from .costs import build_trade_cost_model, max_affordable_buy_quantity, trade_cash_delta
from .futu_runtime import configure_futu_logging


class FutuTradeError(RuntimeError):
    pass


class FutuTransientError(FutuTradeError):
    pass


@dataclass(frozen=True)
class PlannedOrder:
    code: str
    side: str
    quantity: int
    limit_price: float
    reference_price: float
    current_qty: int
    target_qty: int
    target_weight: float


def _price_precision(price_spread: float) -> int:
    rendered = f"{price_spread:.8f}".rstrip("0")
    if "." not in rendered:
        return 0
    return len(rendered.split(".", 1)[1])


def _round_limit_price(raw_price: float, price_spread: float, side: str) -> float:
    spread = price_spread if price_spread and price_spread > 0 else 0.01
    steps = raw_price / spread
    if side == "BUY":
        rounded = math.ceil(steps) * spread
    else:
        rounded = math.floor(steps) * spread
    return round(rounded, _price_precision(spread))


class FutuPaperTrader:
    TERMINAL_ORDER_STATUSES = {
        "FILLED_ALL",
        "CANCELLED_ALL",
        "CANCELLED_PART",
        "FAILED",
        "DISABLED",
        "DELETED",
    }
    TRANSIENT_ERROR_MARKERS = (
        "packeterr.timeout",
        "timeout",
        "timed out",
        "temporarily unavailable",
        "connection reset",
        "connection aborted",
        "connection closed",
        "broken pipe",
        "eof",
        "network is unreachable",
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._futu: Any | None = None
        self.quote_ctx = None
        self.trade_ctx = None

    def __enter__(self) -> "FutuPaperTrader":
        try:
            with socket.create_connection((self.settings.futu_host, self.settings.futu_port), timeout=2):
                pass
        except OSError as exc:
            raise FutuTradeError(
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
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.quote_ctx is not None:
            self.quote_ctx.close()
        if self.trade_ctx is not None:
            self.trade_ctx.close()

    def _expect_ok(self, result: tuple, *, context: str = "Futu call") -> Any:
        ret, payload, *_rest = result
        if ret != self._futu.RET_OK:
            message = str(payload)
            if self._is_transient_error(message):
                raise FutuTransientError(f"{context} failed after retries: {message}")
            raise FutuTradeError(f"{context} failed: {message}")
        return payload

    @classmethod
    def _is_transient_error(cls, message: object) -> bool:
        text = str(message).strip().lower()
        return bool(text) and any(marker in text for marker in cls.TRANSIENT_ERROR_MARKERS)

    @classmethod
    def is_transient_error(cls, message: object) -> bool:
        return cls._is_transient_error(message)

    def _reconnect_contexts(self) -> None:
        if self._futu is None:
            return
        if self.quote_ctx is not None:
            self.quote_ctx.close()
        if self.trade_ctx is not None:
            self.trade_ctx.close()
        self.quote_ctx = self._futu.OpenQuoteContext(
            host=self.settings.futu_host,
            port=self.settings.futu_port,
        )
        self.trade_ctx = self._futu.OpenSecTradeContext(
            filter_trdmarket=getattr(self._futu.TrdMarket, self.settings.futu_trd_market),
            host=self.settings.futu_host,
            port=self.settings.futu_port,
        )

    def _retry_wait(self, attempt: int) -> None:
        wait_seconds = max(0.0, float(self.settings.futu_api_retry_backoff_seconds)) * max(1, attempt)
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    def _call_with_retry(self, context: str, fn, /, *args, **kwargs):
        attempts = max(1, int(self.settings.futu_api_retry_attempts))
        last_message = ""
        for attempt in range(1, attempts + 1):
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                last_message = str(exc)
                if attempt < attempts and self._is_transient_error(last_message):
                    self._reconnect_contexts()
                    self._retry_wait(attempt)
                    continue
                if self._is_transient_error(last_message):
                    raise FutuTransientError(f"{context} failed after {attempts} attempts: {last_message}") from exc
                raise FutuTradeError(f"{context} failed: {last_message}") from exc

            if not isinstance(result, tuple) or not result:
                return result

            ret = result[0]
            if ret == self._futu.RET_OK:
                return result

            payload = result[1] if len(result) > 1 else ""
            last_message = str(payload)
            if attempt < attempts and self._is_transient_error(last_message):
                self._reconnect_contexts()
                self._retry_wait(attempt)
                continue
            if self._is_transient_error(last_message):
                raise FutuTransientError(f"{context} failed after {attempts} attempts: {last_message}")
            raise FutuTradeError(f"{context} failed: {last_message}")

        raise FutuTransientError(f"{context} failed after {attempts} attempts: {last_message or 'unknown error'}")

    def _trade_env(self):
        return getattr(self._futu.TrdEnv, self.settings.futu_trd_env)

    def _trade_market(self):
        return getattr(self._futu.TrdMarket, self.settings.futu_trd_market)

    def _trade_env_name(self) -> str:
        return str(self.settings.futu_trd_env).upper()

    def _is_real_env(self) -> bool:
        return self._trade_env_name() == "REAL"

    def _ensure_real_trading_enabled(self) -> None:
        if not self._is_real_env():
            return
        if not self.settings.futu_enable_real_trading:
            raise FutuTradeError(
                "REAL trading is disabled. Set FUTU_ENABLE_REAL_TRADING=true before submitting live orders."
            )

    def ensure_trade_unlocked(self) -> None:
        if not self._is_real_env():
            return
        self._ensure_real_trading_enabled()
        if not self.settings.futu_unlock_trade_password_md5:
            raise FutuTradeError(
                "REAL trading requires FUTU_UNLOCK_TRADE_PASSWORD_MD5. "
                "Provide the MD5 of your Futu trade password instead of the plain password."
            )
        result = self._call_with_retry(
            "unlock_trade",
            self.trade_ctx.unlock_trade,
            password_md5=self.settings.futu_unlock_trade_password_md5,
        )
        self._expect_ok(result, context="unlock_trade")

    def list_accounts(self) -> pd.DataFrame:
        return self._expect_ok(self._call_with_retry("get_acc_list", self.trade_ctx.get_acc_list), context="get_acc_list")

    def resolve_trade_account(self) -> int:
        accounts = self.list_accounts()
        matching = accounts[accounts["trd_env"] == self._trade_env()]
        matching = matching[
            matching["trdmarket_auth"].apply(
                lambda markets: self.settings.futu_trd_market in markets if isinstance(markets, list) else False
            )
        ]
        if matching.empty:
            raise FutuTradeError(
                f"No Futu {self._trade_env_name()} account found in OpenD for market {self.settings.futu_trd_market}."
            )

        if self.settings.futu_acc_id is not None:
            match = matching[matching["acc_id"] == self.settings.futu_acc_id]
            if match.empty:
                raise FutuTradeError(f"Configured FUTU_ACC_ID={self.settings.futu_acc_id} not found.")
            return int(match.iloc[0]["acc_id"])

        return int(matching.iloc[0]["acc_id"])

    def resolve_sim_account(self) -> int:
        return self.resolve_trade_account()

    @staticmethod
    def _account_cash_available(account: pd.Series, positions: pd.DataFrame | None = None) -> float:
        for column in ("cash", "cash_balance", "available_funds"):
            if column not in account.index:
                continue
            try:
                value = float(account[column])
            except (TypeError, ValueError):
                continue
            if not pd.isna(value):
                return max(0.0, value)

        total_assets = 0.0
        if "total_assets" in account.index:
            try:
                total_assets = float(account["total_assets"])
            except (TypeError, ValueError):
                total_assets = 0.0

        if positions is not None and not positions.empty and "market_val" in positions.columns:
            market_val = float(pd.to_numeric(positions["market_val"], errors="coerce").fillna(0.0).sum())
            return max(0.0, total_assets - market_val)
        return max(0.0, total_assets)

    def get_account_info(self, acc_id: int) -> pd.Series:
        self.ensure_trade_unlocked()
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
        info = self._expect_ok(
            self._call_with_retry(
                "accinfo_query",
                self.trade_ctx.accinfo_query,
                trd_env=self._trade_env(),
                acc_id=acc_id,
                currency=getattr(self._futu.Currency, currency_by_market.get(self.settings.futu_trd_market, "USD")),
            )
            ,
            context="accinfo_query",
        )
        return info.iloc[0]

    def get_positions(self, acc_id: int) -> pd.DataFrame:
        self.ensure_trade_unlocked()
        positions = self._expect_ok(
            self._call_with_retry(
                "position_list_query",
                self.trade_ctx.position_list_query,
                trd_env=self._trade_env(),
                acc_id=acc_id,
            )
            ,
            context="position_list_query",
        )
        if positions.empty:
            return positions
        positions = positions[positions["code"].str.startswith(f"{self.settings.futu_trd_market}.")].copy()
        if positions.empty:
            return positions

        # Futu may keep zero-quantity rows for the same symbol after fills.
        for column in ["qty", "can_sell_qty", "market_val"]:
            if column in positions.columns:
                positions[column] = pd.to_numeric(positions[column], errors="coerce").fillna(0.0)

        active_mask = pd.Series(False, index=positions.index)
        for column in ["qty", "can_sell_qty", "market_val"]:
            if column in positions.columns:
                active_mask |= positions[column].ne(0)

        return positions.loc[active_mask].reset_index(drop=True)

    @staticmethod
    def _position_totals(positions: pd.DataFrame) -> pd.DataFrame:
        if positions.empty:
            return pd.DataFrame(columns=["qty", "can_sell_qty"])

        normalized = positions.copy()
        for column in ["qty", "can_sell_qty"]:
            if column in normalized.columns:
                normalized[column] = pd.to_numeric(normalized[column], errors="coerce").fillna(0.0)
            else:
                normalized[column] = 0.0

        return normalized.groupby("code", as_index=True)[["qty", "can_sell_qty"]].sum()

    def get_order_history(self, acc_id: int, start: str, end: str) -> pd.DataFrame:
        self.ensure_trade_unlocked()
        orders = self._expect_ok(
            self._call_with_retry(
                "history_order_list_query",
                self.trade_ctx.history_order_list_query,
                start=start,
                end=end,
                trd_env=self._trade_env(),
                acc_id=acc_id,
                order_market=self._trade_market(),
            )
            ,
            context="history_order_list_query",
        )
        if orders.empty:
            return orders
        return orders.sort_values(["updated_time", "create_time"], ascending=False).reset_index(drop=True)

    def get_open_orders(self, acc_id: int) -> pd.DataFrame:
        self.ensure_trade_unlocked()
        orders = self._expect_ok(
            self._call_with_retry(
                "order_list_query",
                self.trade_ctx.order_list_query,
                trd_env=self._trade_env(),
                acc_id=acc_id,
                refresh_cache=True,
                order_market=self._trade_market(),
            )
            ,
            context="order_list_query",
        )
        if orders.empty:
            return orders
        if "order_status" in orders.columns:
            orders = orders[~orders["order_status"].isin(self.TERMINAL_ORDER_STATUSES)].copy()
        if orders.empty:
            return orders
        return orders.sort_values(["updated_time", "create_time"], ascending=False).reset_index(drop=True)

    def get_snapshots(self, symbols: list[str]) -> pd.DataFrame:
        snapshot = self._expect_ok(
            self._call_with_retry("get_market_snapshot", self.quote_ctx.get_market_snapshot, symbols),
            context="get_market_snapshot",
        )
        return snapshot.set_index("code")

    def subscribe_realtime(self, symbols: list[str]) -> None:
        result = self._call_with_retry(
            "subscribe_realtime",
            self.quote_ctx.subscribe,
            symbols,
            [self._futu.SubType.K_1M, self._futu.SubType.ORDER_BOOK, self._futu.SubType.TICKER],
            subscribe_push=False,
            session=self._futu.Session.RTH,
        )
        self._expect_ok(result, context="subscribe_realtime")

    def subscribe_types(self, symbols: list[str], subtypes: list[str], *, session: str = "RTH") -> None:
        resolved_subtypes = [getattr(self._futu.SubType, subtype) for subtype in subtypes]
        result = self._call_with_retry(
            "subscribe_types",
            self.quote_ctx.subscribe,
            symbols,
            resolved_subtypes,
            subscribe_push=False,
            session=getattr(self._futu.Session, session),
        )
        self._expect_ok(result, context="subscribe_types")

    def get_recent_klines(self, code: str, num: int) -> pd.DataFrame:
        data = self._expect_ok(
            self._call_with_retry(
                f"get_cur_kline[{code}][K_1M]",
                self.quote_ctx.get_cur_kline,
                code,
                num,
                self._futu.KLType.K_1M,
                self._futu.AuType.QFQ,
            )
            ,
            context=f"get_cur_kline[{code}][K_1M]",
        )
        return data

    def get_daily_klines(self, code: str, num: int) -> pd.DataFrame:
        data = self._expect_ok(
            self._call_with_retry(
                f"get_cur_kline[{code}][K_DAY]",
                self.quote_ctx.get_cur_kline,
                code,
                num,
                self._futu.KLType.K_DAY,
                self._futu.AuType.QFQ,
            )
            ,
            context=f"get_cur_kline[{code}][K_DAY]",
        )
        return data

    def request_history_klines(
        self,
        code: str,
        *,
        start: str | None = None,
        end: str | None = None,
        ktype: str = "K_DAY",
        max_count: int = 1000,
        extended_time: bool = False,
        session: str = "RTH",
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        page_req_key = None
        page_guard = 0

        while True:
            ret, payload, page_req_key = self._call_with_retry(
                f"request_history_kline[{code}][{ktype}]",
                self.quote_ctx.request_history_kline,
                code,
                start=start,
                end=end,
                ktype=getattr(self._futu.KLType, ktype),
                autype=self._futu.AuType.QFQ,
                max_count=max_count,
                page_req_key=page_req_key,
                extended_time=extended_time,
                session=getattr(self._futu.Session, session),
            )
            if payload.empty:
                break
            frames.append(payload)
            page_guard += 1
            if page_req_key is None or page_guard >= 30:
                break

        if not frames:
            return pd.DataFrame()

        history = pd.concat(frames, ignore_index=True)
        if "time_key" in history.columns:
            history = history.drop_duplicates(subset=["time_key"], keep="last").sort_values("time_key").reset_index(drop=True)
        return history

    def get_order_book_safe(self, code: str, depth: int) -> dict[str, Any] | None:
        try:
            ret, payload = self._call_with_retry(f"get_order_book[{code}]", self.quote_ctx.get_order_book, code, depth)
        except FutuTradeError:
            return None
        if ret != self._futu.RET_OK:
            return None
        return payload

    def get_recent_tickers(self, code: str, num: int) -> pd.DataFrame:
        try:
            ret, payload = self._call_with_retry(f"get_rt_ticker[{code}]", self.quote_ctx.get_rt_ticker, code, num)
        except FutuTradeError:
            return pd.DataFrame(columns=["price", "volume", "ticker_direction"])
        if ret != self._futu.RET_OK:
            return pd.DataFrame(columns=["price", "volume", "ticker_direction"])
        return payload

    def build_fixed_order(self, code: str, side: str, quantity: int) -> PlannedOrder | None:
        if quantity <= 0:
            return None

        snapshots = self.get_snapshots([code])
        snapshot = snapshots.loc[code]
        acc_id = self.resolve_trade_account()
        positions = self.get_positions(acc_id)
        account = self.get_account_info(acc_id)
        position_totals = self._position_totals(positions)
        trade_cost_model = build_trade_cost_model(self.settings)
        available_cash = self._account_cash_available(account, positions)

        current_qty = int(float(position_totals.loc[code, "qty"])) if code in position_totals.index else 0
        can_sell_qty = int(float(position_totals.loc[code, "can_sell_qty"])) if code in position_totals.index else 0
        lot_size = int(snapshot.get("lot_size", 1)) or 1
        if quantity < lot_size:
            return None

        order_qty = math.floor(quantity / lot_size) * lot_size
        if side == "SELL":
            order_qty = min(order_qty, can_sell_qty)
        if order_qty <= 0:
            return None

        reference_price = float(snapshot["last_price"])
        if reference_price <= 0:
            raise FutuTradeError(f"Invalid last price for {code}.")

        raw_price = float(snapshot["ask_price"] if side == "BUY" and snapshot["ask_price"] > 0 else snapshot["bid_price"])
        if raw_price <= 0:
            raw_price = reference_price

        buffer = self.settings.futu_price_buffer_bps / 10_000
        buffered_price = raw_price * (1 + buffer if side == "BUY" else 1 - buffer)
        limit_price = _round_limit_price(buffered_price, float(snapshot.get("price_spread", 0.01) or 0.01), side)

        if side == "BUY":
            affordable_qty = max_affordable_buy_quantity(
                available_cash,
                limit_price,
                int(order_qty),
                timestamp=pd.Timestamp.utcnow(),
                lot_size=lot_size,
                model=trade_cost_model,
            )
            order_qty = math.floor(affordable_qty / lot_size) * lot_size
            if order_qty <= 0:
                return None

        target_qty = current_qty + order_qty if side == "BUY" else max(0, current_qty - order_qty)
        return PlannedOrder(
            code=code,
            side=side,
            quantity=int(order_qty),
            limit_price=limit_price,
            reference_price=reference_price,
            current_qty=current_qty,
            target_qty=int(target_qty),
            target_weight=0.0,
        )

    def plan_rebalance(
        self,
        target_weights: dict[str, float],
        *,
        ignore_symbols: set[str] | None = None,
    ) -> tuple[pd.Series, list[PlannedOrder]]:
        acc_id = self.resolve_trade_account()
        account = self.get_account_info(acc_id)
        positions = self.get_positions(acc_id)
        ignored = ignore_symbols or set()
        if ignored and not positions.empty:
            positions = positions[~positions["code"].isin(ignored)].copy()

        held_symbols = positions["code"].tolist() if not positions.empty else []
        target_symbols = {code: weight for code, weight in target_weights.items() if code not in ignored}
        symbols = sorted(set(target_symbols) | set(held_symbols))
        if not symbols:
            raise FutuTradeError("No symbols to rebalance.")

        snapshots = self.get_snapshots(symbols)
        position_totals = self._position_totals(positions)
        total_assets = float(account["total_assets"])
        trade_cost_model = build_trade_cost_model(self.settings)
        available_cash = self._account_cash_available(account, positions)

        planned: list[PlannedOrder] = []
        planned_by_side: dict[str, list[PlannedOrder]] = {"SELL": [], "BUY": []}
        latest_timestamp = pd.Timestamp.utcnow()
        sell_first: list[tuple[str, int, int, int, float, float, float, int]] = []
        buy_second: list[tuple[str, int, int, int, float, float, float, int]] = []
        for code in symbols:
            snapshot = snapshots.loc[code]
            current_qty = int(float(position_totals.loc[code, "qty"])) if code in position_totals.index else 0
            can_sell_qty = int(float(position_totals.loc[code, "can_sell_qty"])) if code in position_totals.index else 0
            lot_size = int(snapshot.get("lot_size", 1)) or 1
            target_weight = float(target_symbols.get(code, 0.0))
            reference_price = float(snapshot["last_price"])
            if reference_price <= 0:
                raise FutuTradeError(f"Invalid last price for {code}.")

            target_value = total_assets * target_weight
            target_qty = math.floor(target_value / reference_price / lot_size) * lot_size
            delta_qty = target_qty - current_qty
            if delta_qty == 0:
                continue

            side = "BUY" if delta_qty > 0 else "SELL"
            order_qty = delta_qty if delta_qty > 0 else min(abs(delta_qty), can_sell_qty)
            if order_qty <= 0:
                continue

            raw_price = float(snapshot["ask_price"] if side == "BUY" and snapshot["ask_price"] > 0 else snapshot["bid_price"])
            if raw_price <= 0:
                raw_price = reference_price

            buffer = self.settings.futu_price_buffer_bps / 10_000
            buffered_price = raw_price * (1 + buffer if side == "BUY" else 1 - buffer)
            limit_price = _round_limit_price(buffered_price, float(snapshot.get("price_spread", 0.01) or 0.01), side)
            row = (code, current_qty, can_sell_qty, lot_size, reference_price, limit_price, target_weight, int(order_qty))
            if side == "SELL":
                sell_first.append(row)
            else:
                buy_second.append(row)

        for code, current_qty, _can_sell_qty, _lot_size, reference_price, limit_price, target_weight, order_qty in sell_first:
            cash_delta, _breakdown = trade_cash_delta(
                "SELL",
                order_qty,
                limit_price,
                timestamp=latest_timestamp,
                model=trade_cost_model,
            )
            available_cash += cash_delta
            planned_by_side["SELL"].append(
                PlannedOrder(
                    code=code,
                    side="SELL",
                    quantity=int(order_qty),
                    limit_price=limit_price,
                    reference_price=reference_price,
                    current_qty=current_qty,
                    target_qty=max(0, int(current_qty - order_qty)),
                    target_weight=target_weight,
                )
            )

        for code, current_qty, _can_sell_qty, lot_size, reference_price, limit_price, target_weight, requested_qty in buy_second:
            affordable_qty = max_affordable_buy_quantity(
                available_cash,
                limit_price,
                int(requested_qty),
                timestamp=latest_timestamp,
                lot_size=lot_size,
                model=trade_cost_model,
            )
            order_qty = math.floor(affordable_qty / lot_size) * lot_size
            if order_qty <= 0:
                continue
            cash_delta, _breakdown = trade_cash_delta(
                "BUY",
                order_qty,
                limit_price,
                timestamp=latest_timestamp,
                model=trade_cost_model,
            )
            available_cash += cash_delta
            planned_by_side["BUY"].append(
                PlannedOrder(
                    code=code,
                    side="BUY",
                    quantity=int(order_qty),
                    limit_price=limit_price,
                    reference_price=reference_price,
                    current_qty=current_qty,
                    target_qty=int(current_qty + order_qty),
                    target_weight=target_weight,
                )
            )

        planned = [*planned_by_side["SELL"], *planned_by_side["BUY"]]
        return account, planned

    def submit_orders(self, orders: list[PlannedOrder]) -> pd.DataFrame:
        self._ensure_real_trading_enabled()
        self.ensure_trade_unlocked()
        acc_id = self.resolve_trade_account()
        rows: list[dict[str, Any]] = []
        for order in orders:
            try:
                ret, payload = self._call_with_retry(
                    f"place_order[{order.code}][{order.side}]",
                    self.trade_ctx.place_order,
                    price=order.limit_price,
                    qty=order.quantity,
                    code=order.code,
                    trd_side=getattr(self._futu.TrdSide, order.side),
                    order_type=self._futu.OrderType.NORMAL,
                    trd_env=self._trade_env(),
                    acc_id=acc_id,
                    time_in_force=self._futu.TimeInForce.DAY,
                    fill_outside_rth=self.settings.futu_fill_outside_rth,
                )
            except FutuTradeError as exc:
                rows.append(
                    {
                        "code": order.code,
                        "side": order.side,
                        "quantity": order.quantity,
                        "limit_price": order.limit_price,
                        "status": "error",
                        "detail": str(exc),
                    }
                )
                continue
            rows.append(
                {
                    "code": order.code,
                    "side": order.side,
                    "quantity": order.quantity,
                    "limit_price": order.limit_price,
                    "status": "submitted" if ret == self._futu.RET_OK else "error",
                    "detail": payload.iloc[0]["order_id"] if ret == self._futu.RET_OK else str(payload),
                }
            )
        return pd.DataFrame(rows)
