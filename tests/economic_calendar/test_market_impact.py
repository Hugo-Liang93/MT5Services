"""MarketImpactAnalyzer 核心逻辑测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.calendar.economic_calendar.market_impact import MarketImpactAnalyzer
from src.clients.economic_calendar import EconomicCalendarEvent


def _make_event(
    event_name: str = "Nonfarm Payrolls",
    scheduled_at: datetime | None = None,
    importance: int = 3,
    actual: str | None = "250",
    forecast: str | None = "200",
    previous: str | None = "180",
) -> EconomicCalendarEvent:
    if scheduled_at is None:
        scheduled_at = datetime(2026, 3, 7, 13, 30, tzinfo=timezone.utc)
    return EconomicCalendarEvent(
        scheduled_at=scheduled_at,
        event_uid=f"te:{event_name}:{scheduled_at.isoformat()}",
        source="tradingeconomics",
        provider_event_id=f"test:{event_name}",
        event_name=event_name,
        country="United States",
        currency="USD",
        importance=importance,
        actual=actual,
        forecast=forecast,
        previous=previous,
        status="released",
        released_at=scheduled_at + timedelta(seconds=30),
    )


def _make_ohlc_bars(
    event_time: datetime, count: int = 300, interval_minutes: int = 1
) -> list:
    """生成模拟 OHLC bars。

    格式: (time, symbol, timeframe, open, high, low, close, volume, ...)
    价格在事件时间前平稳（2000），事件后上涨（2000→2020）。
    """
    bars = []
    base_price = 2000.0
    start = event_time - timedelta(minutes=count // 2)
    for i in range(count):
        bar_time = start + timedelta(minutes=i * interval_minutes)
        if bar_time < event_time:
            price = base_price + (i * 0.01)  # 事件前微小波动
            high = price + 0.5
            low = price - 0.5
        else:
            # 事件后上涨
            elapsed = (bar_time - event_time).total_seconds() / 60
            price = base_price + elapsed * 0.1
            high = price + 1.5
            low = price - 0.5
        # 格式匹配 market_repo: (symbol, timeframe, open, high, low, close, volume, time)
        bars.append((
            "XAUUSD",     # [0] symbol
            "M1",         # [1] timeframe
            price - 0.1,  # [2] open
            high,          # [3] high
            low,           # [4] low
            price,         # [5] close
            100.0,         # [6] volume
            bar_time,     # [7] time
        ))
    return bars


@pytest.mark.unit
class TestMarketImpactCalculations:
    def test_calc_surprise_positive(self):
        event = _make_event(actual="250", forecast="200")
        result = MarketImpactAnalyzer._calc_surprise(event)
        assert result is not None
        assert abs(result - 25.0) < 0.01  # (250-200)/200 * 100 = 25%

    def test_calc_surprise_negative(self):
        event = _make_event(actual="150", forecast="200")
        result = MarketImpactAnalyzer._calc_surprise(event)
        assert result is not None
        assert abs(result - (-25.0)) < 0.01

    def test_calc_surprise_none_when_missing(self):
        event = _make_event(actual=None, forecast="200")
        assert MarketImpactAnalyzer._calc_surprise(event) is None

        event2 = _make_event(actual="250", forecast=None)
        assert MarketImpactAnalyzer._calc_surprise(event2) is None

    def test_calc_surprise_zero_forecast(self):
        event = _make_event(actual="5", forecast="0")
        assert MarketImpactAnalyzer._calc_surprise(event) is None

    def test_find_price_at(self):
        event_time = datetime(2026, 3, 7, 13, 30, tzinfo=timezone.utc)
        bars = _make_ohlc_bars(event_time, count=10)
        price = MarketImpactAnalyzer._find_price_at(bars, event_time)
        assert price is not None
        assert isinstance(price, float)

    def test_find_price_at_empty_bars(self):
        event_time = datetime(2026, 3, 7, 13, 30, tzinfo=timezone.utc)
        assert MarketImpactAnalyzer._find_price_at([], event_time) is None

    def test_calc_window_stats(self):
        # 模拟 3 根 bar: close=[100, 105, 110], high=[102, 107, 112], low=[98, 103, 108]
        # 格式: (symbol, tf, open, high, low, close, volume, time)
        bars = [
            ("X", "M1", 99, 102, 98, 100, 10, None),
            ("X", "M1", 104, 107, 103, 105, 10, None),
            ("X", "M1", 109, 112, 108, 110, 10, None),
        ]
        change, range_val = MarketImpactAnalyzer._calc_window_stats(bars, 100.0)
        assert change == 10.0  # 110 - 100
        assert range_val == 14.0  # 112 - 98

    def test_calc_window_stats_empty(self):
        change, range_val = MarketImpactAnalyzer._calc_window_stats([], 100.0)
        assert change is None
        assert range_val is None


@pytest.mark.unit
class TestMarketImpactAnalyzer:
    def test_disabled_by_default(self):
        settings = MagicMock()
        settings.market_impact_enabled = False
        analyzer = MarketImpactAnalyzer(
            db_writer=MagicMock(), settings=settings
        )
        event = _make_event()
        # 不应注册任何 pending
        analyzer.on_event_released(event)
        assert len(analyzer._pending) == 0

    def test_on_event_released_registers_pending(self):
        settings = MagicMock()
        settings.market_impact_enabled = True
        settings.market_impact_importance_min = 2
        settings.market_impact_symbols = ["XAUUSD"]
        settings.market_impact_timeframes = ["M1"]
        settings.market_impact_post_windows = [5, 15, 30]
        settings.market_impact_final_collection_delay_minutes = 40
        analyzer = MarketImpactAnalyzer(
            db_writer=MagicMock(), settings=settings
        )
        event = _make_event(importance=3)
        analyzer.on_event_released(event)
        assert len(analyzer._pending) == 1

    def test_ignores_low_importance(self):
        settings = MagicMock()
        settings.market_impact_enabled = True
        settings.market_impact_importance_min = 3
        analyzer = MarketImpactAnalyzer(
            db_writer=MagicMock(), settings=settings
        )
        event = _make_event(importance=1)
        analyzer.on_event_released(event)
        assert len(analyzer._pending) == 0

    def test_ignores_all_day_events(self):
        settings = MagicMock()
        settings.market_impact_enabled = True
        settings.market_impact_importance_min = 2
        analyzer = MarketImpactAnalyzer(
            db_writer=MagicMock(), settings=settings
        )
        event = _make_event(importance=3)
        event.all_day = True
        analyzer.on_event_released(event)
        assert len(analyzer._pending) == 0

    def test_collect_impact_with_bars(self):
        event_time = datetime(2026, 3, 7, 13, 30, tzinfo=timezone.utc)
        bars = _make_ohlc_bars(event_time, count=300)

        mock_repo = MagicMock()
        mock_repo.fetch_ohlc_range.return_value = bars

        settings = MagicMock()
        settings.market_impact_enabled = True
        settings.market_impact_pre_windows = [30, 60]
        settings.market_impact_post_windows = [5, 15, 30]

        analyzer = MarketImpactAnalyzer(
            db_writer=MagicMock(),
            market_repo=mock_repo,
            settings=settings,
        )
        event = _make_event(scheduled_at=event_time)
        result = analyzer.collect_impact(event, "XAUUSD", "M1")
        assert result is not None
        assert "pre_price" in result
        assert "pre_30m_change" in result
        assert "post_5m_change" in result
        assert "surprise_pct" in result

    def test_collect_impact_no_repo(self):
        analyzer = MarketImpactAnalyzer(
            db_writer=MagicMock(), market_repo=None
        )
        event = _make_event()
        assert analyzer.collect_impact(event, "XAUUSD", "M1") is None

    def test_stats(self):
        settings = MagicMock()
        settings.market_impact_enabled = True
        settings.market_impact_symbols = ["XAUUSD"]
        settings.market_impact_timeframes = ["M1", "M5"]
        settings.market_impact_importance_min = 2
        analyzer = MarketImpactAnalyzer(
            db_writer=MagicMock(), settings=settings
        )
        s = analyzer.stats()
        assert s["enabled"] is True
        assert s["symbols"] == ["XAUUSD"]
        assert s["pending_analyses"] == 0


@pytest.mark.unit
class TestTradeGuardDynamicBuffer:
    @staticmethod
    def _make_mock_service(
        pre_buffer: int = 30, post_buffer: int = 30, analyzer=None,
    ):
        mock_service = MagicMock()
        mock_service.settings.pre_event_buffer_minutes = pre_buffer
        mock_service.settings.post_event_buffer_minutes = post_buffer
        mock_service.settings.market_impact_high_spike_threshold = 3.0
        mock_service.settings.market_impact_med_spike_threshold = 2.0
        mock_service.settings.market_impact_high_spike_buffer_minutes = 60
        mock_service.settings.market_impact_med_spike_buffer_minutes = 45
        mock_service.market_impact_analyzer = analyzer
        return mock_service

    def test_high_volatility_expands_buffer(self):
        """高波动事件应扩大 Trade Guard 保护窗口。"""
        from src.calendar.economic_calendar.trade_guard import compute_window_bounds

        mock_analyzer = MagicMock()
        mock_analyzer.get_impact_forecast.return_value = {
            "expected_volatility_spike": 4.0,
            "sample_count": 10,
        }
        mock_service = self._make_mock_service(analyzer=mock_analyzer)

        event = _make_event()
        start, end = compute_window_bounds(mock_service, event)

        expected_start = event.scheduled_at - timedelta(minutes=60)
        expected_end = event.scheduled_at + timedelta(minutes=60)
        assert start == expected_start
        assert end == expected_end

    def test_normal_volatility_keeps_default(self):
        """正常波动事件保持默认保护窗口。"""
        from src.calendar.economic_calendar.trade_guard import compute_window_bounds

        mock_analyzer = MagicMock()
        mock_analyzer.get_impact_forecast.return_value = {
            "expected_volatility_spike": 1.5,
            "sample_count": 10,
        }
        mock_service = self._make_mock_service(analyzer=mock_analyzer)

        event = _make_event()
        start, end = compute_window_bounds(mock_service, event)

        expected_start = event.scheduled_at - timedelta(minutes=30)
        expected_end = event.scheduled_at + timedelta(minutes=30)
        assert start == expected_start
        assert end == expected_end

    def test_no_analyzer_keeps_default(self):
        """无 analyzer 时保持默认。"""
        from src.calendar.economic_calendar.trade_guard import compute_window_bounds

        mock_service = self._make_mock_service(analyzer=None)

        event = _make_event()
        start, end = compute_window_bounds(mock_service, event)

        expected_start = event.scheduled_at - timedelta(minutes=30)
        expected_end = event.scheduled_at + timedelta(minutes=30)
        assert start == expected_start
        assert end == expected_end
