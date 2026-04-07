from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.clients.mt5_market import OHLC
from src.market_structure import MarketStructureAnalyzer, MarketStructureConfig


class DummyMarketService:
    def __init__(self, bars):
        self._bars = list(bars)

    def get_ohlc_closed(self, symbol, timeframe, limit=None):
        bars = [bar for bar in self._bars if bar.symbol == symbol and bar.timeframe == timeframe]
        return bars[-limit:] if limit is not None else bars


def _build_bar(timestamp: datetime, *, high: float, low: float, close: float) -> OHLC:
    return OHLC(
        symbol="XAUUSD",
        timeframe="M5",
        time=timestamp,
        open=close,
        high=high,
        low=low,
        close=close,
        volume=1.0,
        indicators={},
    )


def test_market_structure_analyzer_builds_breakout_context() -> None:
    day0 = datetime(2026, 3, 18, 0, 0, tzinfo=timezone.utc)
    day1 = datetime(2026, 3, 19, 0, 0, tzinfo=timezone.utc)
    bars = []

    for index in range(24):
        ts = day0 + timedelta(hours=index)
        bars.append(_build_bar(ts, high=3002.0 + index * 0.05, low=2990.0 - index * 0.02, close=2998.0))

    for hour in range(0, 7):
        ts = day1 + timedelta(hours=hour)
        bars.append(_build_bar(ts, high=2999.0, low=2994.0, close=2997.0))

    for slot in range(12):
        ts = day1 + timedelta(hours=7, minutes=slot * 5)
        bars.append(_build_bar(ts, high=3001.0, low=2998.5, close=3000.5))

    for slot in range(18):
        ts = day1 + timedelta(hours=8, minutes=slot * 5)
        bars.append(_build_bar(ts, high=3000.8, low=2999.9, close=3000.2))

    analyzer = MarketStructureAnalyzer(
        DummyMarketService(bars),
        config=MarketStructureConfig(
            enabled=True,
            lookback_bars=128,
            open_range_minutes=60,
            compression_window_bars=6,
            compression_reference_bars=24,
        ),
    )

    result = analyzer.analyze(
        "XAUUSD",
        "M5",
        event_time=day1 + timedelta(hours=8, minutes=55),
        latest_close=3005.0,
    )

    assert result["current_session"] == "london"
    assert result["breakout_state"] == "above_previous_day_high"
    assert result["range_reference"] == "previous_day_high"
    assert result["previous_day_high"] is not None
    assert result["asia_range_high"] == 2999.0
    assert result["london_open_high"] == 3001.0
    assert result["compression_state"] in {"contracted", "normal", "expanded"}


def test_market_structure_analyzer_detects_reclaim() -> None:
    base = datetime(2026, 3, 19, 6, 0, tzinfo=timezone.utc)
    bars = [
        _build_bar(base - timedelta(days=1), high=3004.0, low=2992.0, close=2998.0),
        _build_bar(base, high=3000.0, low=2995.0, close=2998.0),
        _build_bar(base + timedelta(minutes=5), high=3005.5, low=2998.0, close=2999.2),
    ]
    analyzer = MarketStructureAnalyzer(DummyMarketService(bars))

    result = analyzer.analyze(
        "XAUUSD",
        "M5",
        event_time=base + timedelta(minutes=5),
        latest_close=2999.2,
    )

    assert result["reclaim_state"] == "bearish_reclaim_previous_day_high"
    assert result["sweep_state"] == "bearish_reclaim_previous_day_high"
    assert "bearish_reclaim_previous_day_high" in result["swept_levels"]
    assert result["structure_bias"] == "bearish_reclaim"


def test_market_structure_analyzer_tracks_new_york_open_range_levels() -> None:
    day0 = datetime(2026, 3, 18, 0, 0, tzinfo=timezone.utc)
    day1 = datetime(2026, 3, 19, 0, 0, tzinfo=timezone.utc)
    bars = []

    for index in range(24):
        ts = day0 + timedelta(hours=index)
        bars.append(_build_bar(ts, high=3001.0, low=2991.0, close=2998.0))

    for hour in range(0, 7):
        ts = day1 + timedelta(hours=hour)
        bars.append(_build_bar(ts, high=2998.0, low=2994.0, close=2996.0))

    for slot in range(12):
        ts = day1 + timedelta(hours=13, minutes=slot * 5)
        bars.append(_build_bar(ts, high=3004.0, low=3000.0, close=3002.5))

    analyzer = MarketStructureAnalyzer(DummyMarketService(bars))

    result = analyzer.analyze(
        "XAUUSD",
        "M5",
        event_time=day1 + timedelta(hours=13, minutes=55),
        latest_close=3005.0,
    )

    assert result["current_session"] == "new_york"
    assert result["new_york_open_high"] == 3004.0
    assert result["new_york_open_low"] == 3000.0
    assert "above_new_york_open_high" in result["breached_levels"]


def test_market_structure_analyzer_detects_new_york_open_reclaim() -> None:
    day0 = datetime(2026, 3, 18, 0, 0, tzinfo=timezone.utc)
    day1 = datetime(2026, 3, 19, 0, 0, tzinfo=timezone.utc)
    bars = []

    for index in range(24):
        ts = day0 + timedelta(hours=index)
        bars.append(_build_bar(ts, high=3001.0, low=2990.0, close=2997.0))

    for slot in range(12):
        ts = day1 + timedelta(hours=13, minutes=slot * 5)
        bars.append(_build_bar(ts, high=3004.0, low=3000.0, close=3002.0))

    bars.append(
        _build_bar(
            day1 + timedelta(hours=14),
            high=3005.5,
            low=3002.0,
            close=3003.5,
        )
    )

    analyzer = MarketStructureAnalyzer(DummyMarketService(bars))

    result = analyzer.analyze(
        "XAUUSD",
        "M5",
        event_time=day1 + timedelta(hours=14),
        latest_close=3003.5,
    )

    assert result["reclaim_state"] == "bearish_reclaim_new_york_open_high"
    assert result["sweep_state"] == "bearish_reclaim_new_york_open_high"
    assert "bearish_reclaim_new_york_open_high" in result["swept_levels"]


def test_market_structure_analyzer_detects_bullish_first_pullback() -> None:
    day0 = datetime(2026, 3, 18, 0, 0, tzinfo=timezone.utc)
    day1 = datetime(2026, 3, 19, 0, 0, tzinfo=timezone.utc)
    bars = []

    for index in range(24):
        ts = day0 + timedelta(hours=index)
        bars.append(_build_bar(ts, high=3001.0, low=2990.0, close=2997.0))

    bars.append(_build_bar(day1 + timedelta(hours=8), high=3005.0, low=3002.0, close=3004.0))
    bars.append(_build_bar(day1 + timedelta(hours=8, minutes=5), high=3004.5, low=3000.5, close=3002.5))

    analyzer = MarketStructureAnalyzer(DummyMarketService(bars))

    result = analyzer.analyze(
        "XAUUSD",
        "M5",
        event_time=day1 + timedelta(hours=8, minutes=5),
        latest_close=3002.5,
    )

    assert result["first_pullback_state"] == "bullish_first_pullback_previous_day_high"
    assert result["pullback_reference"] == "previous_day_high"
    assert result["structure_bias"] == "bullish_pullback"


def test_market_structure_analyzer_detects_bearish_sweep_confirmation() -> None:
    day0 = datetime(2026, 3, 18, 0, 0, tzinfo=timezone.utc)
    day1 = datetime(2026, 3, 19, 0, 0, tzinfo=timezone.utc)
    bars = []

    for index in range(24):
        ts = day0 + timedelta(hours=index)
        bars.append(_build_bar(ts, high=3004.0, low=2990.0, close=2998.0))

    bars.append(
        _build_bar(
            day1 + timedelta(hours=8),
            high=3005.5,
            low=2998.0,
            close=3001.5,
        )
    )
    # 当前 bar：close 低于 sweep bar 的 close，确认看跌
    bars.append(
        _build_bar(
            day1 + timedelta(hours=8, minutes=5),
            high=3001.8,
            low=2999.5,
            close=3000.5,
        )
    )

    analyzer = MarketStructureAnalyzer(DummyMarketService(bars))

    result = analyzer.analyze(
        "XAUUSD",
        "M5",
        event_time=day1 + timedelta(hours=8, minutes=5),
        latest_close=3000.5,
    )

    assert (
        result["sweep_confirmation_state"]
        == "bearish_sweep_confirmed_previous_day_high"
    )
    assert result["confirmation_reference"] == "previous_day_high"
    assert result["structure_bias"] == "bearish_sweep_confirmed"
