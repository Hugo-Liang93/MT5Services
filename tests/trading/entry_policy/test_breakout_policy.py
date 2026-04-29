"""BreakoutEntryPolicy 单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.trading.entry_policy import (
    BarSnapshot,
    BreakoutEntryPolicy,
    EntryIntent,
    EntryType,
    MarketSnapshot,
    PatternType,
)


def _intent(direction: str = "buy") -> EntryIntent:
    return EntryIntent(
        strategy_name="test",
        timeframe="H1",
        direction=direction,  # type: ignore[arg-type]
        confidence=0.6,
        bar_time=datetime(2026, 4, 30, tzinfo=timezone.utc),
        pattern_type=PatternType.BREAKOUT_BULL,
    )


class TestBreakoutPolicy:
    def test_buy_uses_recent_high_plus_buffer(self):
        bar = BarSnapshot(open=4500.0, high=4520.0, low=4490.0, close=4515.0)
        market = MarketSnapshot(
            recent_bars=(bar,), atr_value=10.0, current_close=4515.0
        )
        policy = BreakoutEntryPolicy()
        group = policy.derive(_intent("buy"), market, {})
        member = group.members[0]
        assert member.entry_type == EntryType.STOP
        assert member.member_id == "stop_breakout"
        # extreme=4520, trigger=4520 + 0.05 * 10 = 4520.5
        assert member.trigger_price == pytest.approx(4520.5)
        assert group.metadata["extreme"] == pytest.approx(4520.0)

    def test_sell_uses_recent_low_minus_buffer(self):
        bar = BarSnapshot(open=4500.0, high=4520.0, low=4490.0, close=4495.0)
        market = MarketSnapshot(
            recent_bars=(bar,), atr_value=10.0, current_close=4495.0
        )
        policy = BreakoutEntryPolicy()
        group = policy.derive(_intent("sell"), market, {})
        # extreme=4490, trigger=4490 - 0.05 * 10 = 4489.5
        assert group.members[0].trigger_price == pytest.approx(4489.5)

    def test_lookback_3_uses_max_high_across_bars(self):
        b1 = BarSnapshot(open=4500.0, high=4530.0, low=4495.0, close=4525.0)
        b2 = BarSnapshot(open=4525.0, high=4540.0, low=4520.0, close=4535.0)
        b3 = BarSnapshot(
            open=4535.0, high=4520.0, low=4515.0, close=4518.0
        )  # lower high
        market = MarketSnapshot(
            recent_bars=(b1, b2, b3), atr_value=10.0, current_close=4518.0
        )
        policy = BreakoutEntryPolicy()
        group = policy.derive(_intent("buy"), market, {"lookback_bars": 3})
        # extreme = max(4530, 4540, 4520) = 4540, trigger = 4540 + 0.05 * 10 = 4540.5
        assert group.members[0].trigger_price == pytest.approx(4540.5)

    def test_buffer_atr_override(self):
        bar = BarSnapshot(open=4500.0, high=4520.0, low=4490.0, close=4515.0)
        market = MarketSnapshot(
            recent_bars=(bar,), atr_value=10.0, current_close=4515.0
        )
        policy = BreakoutEntryPolicy()
        group = policy.derive(_intent("buy"), market, {"buffer_atr": 0.20})
        # trigger = 4520 + 0.20 * 10 = 4522
        assert group.members[0].trigger_price == pytest.approx(4522.0)

    def test_describe(self):
        d = BreakoutEntryPolicy().describe()
        assert d["name"] == "breakout"
        assert "lookback_bars" in d["params_schema"]
        assert "buffer_atr" in d["params_schema"]
