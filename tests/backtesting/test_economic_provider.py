"""economic_provider.py 单元测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from src.backtesting.filtering.economic import (
    BacktestTradeGuardProvider,
    _SimpleEvent,
    _SimpleSettings,
)


def _make_event(
    name: str = "NFP",
    importance: int = 3,
    hours_offset: float = 0,
    currency: str = "USD",
    country: str = "United States",
) -> _SimpleEvent:
    base = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
    return _SimpleEvent(
        event_uid=f"test:{name}",
        event_name=name,
        source="test",
        country=country,
        currency=currency,
        importance=importance,
        scheduled_at=base + timedelta(hours=hours_offset),
        status="released",
        session_bucket="new_york",
    )


class TestBacktestTradeGuardProvider:
    def test_empty_events_returns_none_severity(self) -> None:
        provider = BacktestTradeGuardProvider([], _SimpleSettings())
        result = provider.get_trade_guard(
            symbol="XAUUSD",
            at_time=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
            lookahead_minutes=30,
            lookback_minutes=15,
        )
        assert result["severity"] == "none"

    def test_high_importance_event_blocks(self) -> None:
        events = [_make_event("NFP", importance=3)]
        provider = BacktestTradeGuardProvider(events, _SimpleSettings())
        # 在事件时间点查询
        result = provider.get_trade_guard(
            symbol="XAUUSD",
            at_time=events[0].scheduled_at,
            lookahead_minutes=30,
            lookback_minutes=15,
            importance_min=2,
        )
        assert result["severity"] == "block"
        assert result["blocked"] is True

    def test_low_importance_event_warns(self) -> None:
        events = [_make_event("ADP Employment", importance=2)]
        provider = BacktestTradeGuardProvider(events, _SimpleSettings())
        result = provider.get_trade_guard(
            symbol="XAUUSD",
            at_time=events[0].scheduled_at,
            lookahead_minutes=30,
            lookback_minutes=15,
            importance_min=2,
        )
        # importance=2 < block_min=3 → warn
        assert result["severity"] in ("warn", "none")

    def test_event_outside_window_is_safe(self) -> None:
        events = [_make_event("NFP", importance=3)]
        provider = BacktestTradeGuardProvider(events, _SimpleSettings())
        # 事件前 2 小时查询（远在 lookahead 之外）
        far_before = events[0].scheduled_at - timedelta(hours=2)
        result = provider.get_trade_guard(
            symbol="XAUUSD",
            at_time=far_before,
            lookahead_minutes=30,
            lookback_minutes=15,
            importance_min=2,
        )
        assert result["severity"] == "none"

    def test_get_events_filters_by_time(self) -> None:
        events = [
            _make_event("Early", hours_offset=-5),
            _make_event("Middle", hours_offset=0),
            _make_event("Late", hours_offset=5),
        ]
        provider = BacktestTradeGuardProvider(events, _SimpleSettings())
        base = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
        filtered = provider.get_events(
            start_time=base - timedelta(hours=1),
            end_time=base + timedelta(hours=1),
        )
        assert len(filtered) == 1
        assert filtered[0].event_name == "Middle"

    def test_get_events_filters_by_currency(self) -> None:
        events = [
            _make_event("USD Event", currency="USD"),
            _make_event("EUR Event", currency="EUR"),
        ]
        provider = BacktestTradeGuardProvider(events, _SimpleSettings())
        filtered = provider.get_events(currencies=["USD"])
        assert len(filtered) == 1
        assert filtered[0].currency == "USD"

    def test_get_events_filters_by_importance(self) -> None:
        events = [
            _make_event("High", importance=3),
            _make_event("Low", importance=1),
        ]
        provider = BacktestTradeGuardProvider(events, _SimpleSettings())
        filtered = provider.get_events(importance_min=2)
        assert len(filtered) == 1
        assert filtered[0].event_name == "High"


class TestGoldRelevanceFilter:
    def test_relevance_downgrades_non_gold_events(self) -> None:
        """非黄金相关事件的 importance 被降级后不再 block。"""
        events = [_make_event("Consumer Confidence", importance=3)]
        settings = _SimpleSettings(
            trade_guard_relevance_filter_enabled=True,
            gold_impact_keywords="NFP,FOMC,CPI,Fed",
        )
        provider = BacktestTradeGuardProvider(events, settings)
        result = provider.get_trade_guard(
            symbol="XAUUSD",
            at_time=events[0].scheduled_at,
            lookahead_minutes=30,
            lookback_minutes=15,
            importance_min=2,
        )
        # "Consumer Confidence" 不匹配关键词 → importance 3→2 → warn 而非 block
        assert result["severity"] != "block"

    def test_relevance_keeps_gold_events(self) -> None:
        """黄金相关事件保持原始 importance。"""
        events = [_make_event("FOMC Interest Rate Decision", importance=3)]
        settings = _SimpleSettings(
            trade_guard_relevance_filter_enabled=True,
            gold_impact_keywords="NFP,FOMC,CPI,Fed",
        )
        provider = BacktestTradeGuardProvider(events, settings)
        result = provider.get_trade_guard(
            symbol="XAUUSD",
            at_time=events[0].scheduled_at,
            lookahead_minutes=30,
            lookback_minutes=15,
            importance_min=2,
        )
        assert result["severity"] == "block"

    def test_relevance_disabled_keeps_all(self) -> None:
        """关闭 relevance 过滤时所有事件保持原始 importance。"""
        events = [_make_event("Consumer Confidence", importance=3)]
        settings = _SimpleSettings(trade_guard_relevance_filter_enabled=False)
        provider = BacktestTradeGuardProvider(events, settings)
        result = provider.get_trade_guard(
            symbol="XAUUSD",
            at_time=events[0].scheduled_at,
            lookahead_minutes=30,
            lookback_minutes=15,
            importance_min=2,
        )
        assert result["severity"] == "block"
