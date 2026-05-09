"""yfinance ExternalDataSource — fetch daily OHLCV from Yahoo Finance.

Implements the ExternalDataSource Protocol from src/research/external/protocol.py.
Auto-registers under the name "yfinance" at import time so the generic
backfill CLI can dispatch via `--source yfinance`.

NaN volumes normalized to 0.0 (Yahoo returns NaN for symbols without volume,
e.g., index tickers like ^TNX).

NOT responsible for: persistence, retries beyond yfinance defaults, multi-symbol
parallel fetching, rate limiting.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, List

import pandas as pd

from src.research.external.protocol import DailyBar, register_source


class YFinanceError(RuntimeError):
    """Raised when yfinance returns no data or a malformed response."""


class YFinanceClient:
    """Daily OHLCV via yfinance package. ExternalDataSource implementation."""

    name = "yfinance"

    def fetch_daily(self, symbol: str, *, start: date, end: date) -> List[DailyBar]:
        """Fetch daily OHLCV for [start, end] inclusive.

        Raises YFinanceError if the response is empty (Yahoo returns empty
        DataFrame for invalid symbols silently).
        """
        df = self._history_for(symbol, start=start, end=end)
        if df is None or df.empty:
            raise YFinanceError(
                f"yfinance returned no data for {symbol} {start}..{end}"
            )

        bars: List[DailyBar] = []
        for ts, row in df.iterrows():
            if isinstance(ts, pd.Timestamp):
                bar_date = ts.date()
            elif isinstance(ts, date):
                bar_date = ts
            else:
                raise YFinanceError(
                    f"unexpected index type {type(ts).__name__} for {symbol}"
                )

            raw_volume = row.get("Volume")
            if raw_volume is None or pd.isna(raw_volume):
                volume = 0.0
            else:
                volume = float(raw_volume)

            bars.append(
                DailyBar(
                    symbol=symbol,
                    date=bar_date,
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=volume,
                )
            )
        return bars

    def supports_symbol(self, symbol: str) -> bool:
        """yfinance accepts any string; invalid symbols fail at fetch time."""
        return bool(symbol)

    def _history_for(self, symbol: str, *, start: date, end: date) -> Any:
        """Isolated for test patching. Calls yfinance.Ticker(...).history(...)."""
        import yfinance as yf

        # yfinance end is exclusive; bump by 1 day so the range is inclusive
        # (matches our DailyBar contract).
        return yf.Ticker(symbol).history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            interval="1d",
            auto_adjust=False,
            actions=False,
        )


# Register at import time so `get_source("yfinance")` works after this module
# is loaded (typically when src/research/external/__init__.py imports it).
register_source("yfinance", lambda: YFinanceClient())
