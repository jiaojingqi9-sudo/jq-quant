from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import pandas as pd
import yfinance as yf

from .futu_runtime import configure_futu_logging


class MarketDataError(RuntimeError):
    pass


class HistoricalDataProvider(Protocol):
    def fetch_daily_closes(
        self,
        symbols: tuple[str, ...] | list[str],
        start: str,
        end: str | None = None,
    ) -> pd.DataFrame: ...


def futu_code_to_yfinance(symbol: str) -> str:
    market, code = symbol.split(".", 1)
    if market == "US":
        if code == "VIX":
            return "^VIX"
        return code
    if market == "HK":
        return f"{int(code):04d}.HK"
    if market == "SH":
        return f"{code}.SS"
    if market == "SZ":
        return f"{code}.SZ"
    raise MarketDataError(f"Unsupported symbol for yfinance conversion: {symbol}")


def _coerce_close_frame(raw: pd.DataFrame | pd.Series, symbols: list[str]) -> pd.DataFrame:
    if isinstance(raw, pd.Series):
        return raw.to_frame(name=symbols[0])

    if isinstance(raw.columns, pd.MultiIndex):
        level0 = set(raw.columns.get_level_values(0))
        level1 = set(raw.columns.get_level_values(1))
        if "Close" in level0:
            return raw.xs("Close", axis=1, level=0)
        if "Close" in level1:
            return raw.xs("Close", axis=1, level=1)

    if "Close" in raw.columns:
        return raw[["Close"]].rename(columns={"Close": symbols[0]})

    raise MarketDataError("Unable to locate Close prices in downloaded market data.")


@dataclass
class YFinanceDataProvider:
    def fetch_daily_closes(
        self,
        symbols: tuple[str, ...] | list[str],
        start: str,
        end: str | None = None,
    ) -> pd.DataFrame:
        symbol_list = list(symbols)
        ticker_map = {futu_code_to_yfinance(symbol): symbol for symbol in symbol_list}
        raw = yf.download(
            tickers=list(ticker_map.keys()),
            start=start,
            end=end,
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        if raw.empty:
            raise MarketDataError("No market data returned from yfinance.")

        close_frame = _coerce_close_frame(raw, list(ticker_map.keys()))
        close_frame = close_frame.rename(columns=ticker_map)
        close_frame.index = pd.to_datetime(close_frame.index).tz_localize(None)
        close_frame = close_frame.sort_index().dropna(how="all")
        return close_frame[symbol_list]


@dataclass
class FutuQuoteDataProvider:
    host: str
    port: int

    def fetch_daily_closes(
        self,
        symbols: tuple[str, ...] | list[str],
        start: str,
        end: str | None = None,
    ) -> pd.DataFrame:
        import futu

        configure_futu_logging(futu)
        from futu import AuType, KLType, KL_FIELD, OpenQuoteContext, RET_OK, Session

        close_frames: dict[str, pd.Series] = {}
        quote_ctx = OpenQuoteContext(host=self.host, port=self.port)
        try:
            for symbol in symbols:
                page_req_key = None
                chunks: list[pd.DataFrame] = []
                while True:
                    ret, data, page_req_key = quote_ctx.request_history_kline(
                        code=symbol,
                        start=start,
                        end=end,
                        ktype=KLType.K_DAY,
                        autype=AuType.QFQ,
                        fields=[KL_FIELD.DATE_TIME, KL_FIELD.CLOSE],
                        max_count=1000,
                        page_req_key=page_req_key,
                        session=Session.RTH,
                    )
                    if ret != RET_OK:
                        raise MarketDataError(f"Futu history request failed for {symbol}: {data}")
                    chunks.append(data[["time_key", "close"]].copy())
                    if page_req_key is None:
                        break

                merged = pd.concat(chunks, ignore_index=True)
                merged["date"] = pd.to_datetime(merged["time_key"]).dt.normalize()
                close_frames[symbol] = merged.set_index("date")["close"].sort_index()
        finally:
            quote_ctx.close()

        prices = pd.DataFrame(close_frames).sort_index().dropna(how="all")
        return prices[list(symbols)]
