from __future__ import annotations

from datetime import datetime, timezone

from src.api.market import ohlc, ohlc_history, ticks


class _Tick:
    def __init__(self) -> None:
        self.symbol = "XAUUSD"
        self.price = 3025.5
        self.volume = 1.2
        self.time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.time_msc = 1735689600000


class _Bar:
    def __init__(self) -> None:
        self.symbol = "XAUUSD"
        self.timeframe = "M1"
        self.time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.open = 3000.0
        self.high = 3005.0
        self.low = 2995.0
        self.close = 3002.0
        self.volume = 10.0
        self.indicators = {"ema20": {"ema": 3001.0}}


class _MarketService:
    def get_ticks(self, symbol, limit):
        return [_Tick()]

    def get_ohlc_closed(self, symbol, timeframe, limit):
        return [_Bar()]

    def get_ohlc_history_result(self, symbol, timeframe, start_time, end_time, limit, strict=False):
        return ([_Bar()], "cache")


def test_ticks_endpoint_serializes_time_without_duplicate_keyword() -> None:
    response = ticks(symbol="XAUUSD", limit=20, service=_MarketService())

    assert response.success is True
    assert response.data[0].time == "2026-01-01T00:00:00+00:00"


def test_ohlc_endpoint_serializes_time_without_duplicate_keyword() -> None:
    response = ohlc(symbol="XAUUSD", timeframe="M1", limit=20, service=_MarketService())

    assert response.success is True
    assert response.data[0].time == "2026-01-01T00:00:00+00:00"


def test_ohlc_history_endpoint_serializes_time_without_duplicate_keyword() -> None:
    response = ohlc_history(
        symbol="XAUUSD",
        timeframe="M1",
        start_time=None,
        end_time=None,
        limit=20,
        service=_MarketService(),
    )

    assert response.success is True
    assert response.data[0].time == "2026-01-01T00:00:00+00:00"
