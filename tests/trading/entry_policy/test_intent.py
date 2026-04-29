"""EntryIntent / MarketSnapshot / BarSnapshot 输入 DTO 单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.trading.entry_policy import (
    BarSnapshot,
    EntryIntent,
    MarketSnapshot,
    PatternType,
)


class TestBarSnapshot:
    def test_from_mapping_basic(self):
        bar = BarSnapshot.from_mapping(
            {
                "open": 4500.0,
                "high": 4520.0,
                "low": 4490.0,
                "close": 4515.0,
                "volume": 100.0,
            }
        )
        assert bar.open == 4500.0
        assert bar.high == 4520.0
        assert bar.low == 4490.0
        assert bar.close == 4515.0
        assert bar.volume == 100.0
        assert bar.body_low == 4500.0
        assert bar.body_high == 4515.0
        assert bar.total_range == 30.0
        assert bar.body_range == 15.0
        assert bar.upper_wick == pytest.approx(5.0)
        assert bar.lower_wick == pytest.approx(10.0)
        assert bar.mid == pytest.approx(4505.0)

    def test_from_mapping_volume_optional(self):
        bar = BarSnapshot.from_mapping(
            {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}
        )
        assert bar.volume == 0.0

    def test_bear_bar(self):
        bar = BarSnapshot.from_mapping(
            {"open": 4515.0, "high": 4520.0, "low": 4490.0, "close": 4500.0}
        )
        assert bar.body_low == 4500.0
        assert bar.body_high == 4515.0
        assert bar.upper_wick == pytest.approx(5.0)
        assert bar.lower_wick == pytest.approx(10.0)


class TestMarketSnapshot:
    def _bars(self, n: int = 3) -> tuple[BarSnapshot, ...]:
        return tuple(
            BarSnapshot.from_mapping(
                {
                    "open": 4500.0 + i,
                    "high": 4510.0 + i,
                    "low": 4490.0 + i,
                    "close": 4505.0 + i,
                }
            )
            for i in range(n)
        )

    def test_signal_bar_is_last(self):
        bars = self._bars(3)
        snap = MarketSnapshot(recent_bars=bars, atr_value=10.0, current_close=4520.0)
        assert snap.signal_bar is bars[-1]

    def test_prev_bar_is_second_to_last(self):
        bars = self._bars(3)
        snap = MarketSnapshot(recent_bars=bars, atr_value=10.0, current_close=4520.0)
        assert snap.prev_bar is bars[-2]

    def test_prev_bar_none_when_only_one_bar(self):
        bars = self._bars(1)
        snap = MarketSnapshot(recent_bars=bars, atr_value=10.0, current_close=4520.0)
        assert snap.prev_bar is None

    def test_atr_value_must_be_positive(self):
        with pytest.raises(ValueError, match="atr_value must be > 0"):
            MarketSnapshot(
                recent_bars=self._bars(1), atr_value=0.0, current_close=4500.0
            )

    def test_recent_bars_cannot_be_empty(self):
        with pytest.raises(ValueError, match="must contain at least 1 bar"):
            MarketSnapshot(recent_bars=(), atr_value=10.0, current_close=4500.0)


class TestEntryIntent:
    def test_construction(self):
        intent = EntryIntent(
            strategy_name="structured_price_action",
            timeframe="M15",
            direction="buy",
            confidence=0.65,
            bar_time=datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc),
            pattern_type=PatternType.PIN_BULL,
            signal_metadata={"why": "structure_up", "when": "pin_bar_bull"},
        )
        assert intent.strategy_name == "structured_price_action"
        assert intent.pattern_type == PatternType.PIN_BULL
        assert intent.signal_metadata["why"] == "structure_up"
