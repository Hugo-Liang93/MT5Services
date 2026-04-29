"""FibPullbackEntryPolicy 单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.trading.entry_policy import (
    BarSnapshot,
    EntryIntent,
    EntryType,
    FibPullbackEntryPolicy,
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
        pattern_type=PatternType.TRENDLINE_TOUCH_BULL,
    )


def _swing_bars() -> tuple[BarSnapshot, ...]:
    """构造 swing range = [4400, 4600] 的 bars。"""
    return (
        BarSnapshot(open=4500.0, high=4600.0, low=4480.0, close=4580.0),  # swing high
        BarSnapshot(open=4580.0, high=4590.0, low=4400.0, close=4420.0),  # swing low
        BarSnapshot(open=4420.0, high=4530.0, low=4410.0, close=4520.0),  # signal bar
    )


class TestFibPullback:
    def test_buy_picks_first_unbreached_fib_level(self):
        bars = _swing_bars()
        # swing_high=4600, swing_low=4400, range=200
        # current_close=4520
        # fib levels: 0.382=4523.6, 0.5=4500, 0.618=4476.4
        # 0.382: candidate=4600-200*0.382=4523.6 → current 4520 < candidate → 不选
        # 0.5: candidate=4500 → current 4520 > 4500 → 选这个
        # 0.618: candidate=4476.4 → current 4520 > 4476.4 → 更深档位选
        # 实现是逐个 level 更新 chosen，所以最终选 0.618（最深的未被穿透）
        market = MarketSnapshot(recent_bars=bars, atr_value=10.0, current_close=4520.0)
        policy = FibPullbackEntryPolicy()
        group = policy.derive(_intent("buy"), market, {})
        member = group.members[0]
        assert member.entry_type == EntryType.LIMIT
        assert member.trigger_price == pytest.approx(4476.4, abs=0.1)
        assert group.metadata["fib_level"] == 0.618

    def test_sell_uses_swing_low_plus_fib(self):
        bars = (
            BarSnapshot(
                open=4500.0, high=4520.0, low=4400.0, close=4420.0
            ),  # swing low
            BarSnapshot(
                open=4420.0, high=4600.0, low=4410.0, close=4580.0
            ),  # swing high
            BarSnapshot(
                open=4580.0, high=4590.0, low=4470.0, close=4480.0
            ),  # signal bar
        )
        market = MarketSnapshot(recent_bars=bars, atr_value=10.0, current_close=4480.0)
        policy = FibPullbackEntryPolicy()
        group = policy.derive(_intent("sell"), market, {})
        # swing_high=4600, swing_low=4400, range=200, current=4480
        # sell: candidate = swing_low + level * range
        # 0.382: 4400 + 76.4 = 4476.4 → 4480 > 4476.4 → 不是 "current<anchor"
        # 0.5: 4400 + 100 = 4500 → 4480 < 4500 → 选
        # 0.618: 4400 + 123.6 = 4523.6 → 4480 < 4523.6 → 选更深
        assert group.members[0].trigger_price == pytest.approx(4523.6, abs=0.1)
        assert group.metadata["fib_level"] == 0.618

    def test_custom_fib_levels(self):
        bars = _swing_bars()
        market = MarketSnapshot(recent_bars=bars, atr_value=10.0, current_close=4520.0)
        policy = FibPullbackEntryPolicy()
        group = policy.derive(
            _intent("buy"),
            market,
            {"fib_levels": [0.236, 0.382]},
        )
        # 0.236: 4600 - 47.2 = 4552.8 → 4520 < 4552.8 → 不选
        # 0.382: 4600 - 76.4 = 4523.6 → 4520 < 4523.6 → 不选
        # 全部穿透 → 用最深档位 0.382 → trigger=4523.6
        assert group.members[0].trigger_price == pytest.approx(4523.6, abs=0.1)

    def test_swing_lookback_param(self):
        bars = _swing_bars()
        market = MarketSnapshot(recent_bars=bars, atr_value=10.0, current_close=4520.0)
        policy = FibPullbackEntryPolicy()
        group = policy.derive(
            _intent("buy"),
            market,
            {"swing_lookback": 2},  # 只看最后 2 根
        )
        # 最后 2 根 high: 4590, 4530 → max=4590
        # 最后 2 根 low: 4400, 4410 → min=4400
        # range=190
        assert group.metadata["lookback_bars"] == 2

    def test_fallback_when_swing_range_zero(self):
        # 所有 bar 完全相同
        bar = BarSnapshot(open=4500.0, high=4500.0, low=4500.0, close=4500.0)
        market = MarketSnapshot(
            recent_bars=(bar, bar, bar), atr_value=10.0, current_close=4500.0
        )
        policy = FibPullbackEntryPolicy()
        group = policy.derive(_intent("buy"), market, {})
        # range=0 → fallback to current_close
        assert group.members[0].trigger_price == pytest.approx(4500.0)

    def test_invalid_fib_levels_falls_back_default(self):
        bars = _swing_bars()
        market = MarketSnapshot(recent_bars=bars, atr_value=10.0, current_close=4520.0)
        policy = FibPullbackEntryPolicy()
        # fib_levels 都是 >=1 的非法值
        group = policy.derive(
            _intent("buy"),
            market,
            {"fib_levels": [1.5, 2.0]},
        )
        # 全过滤后空 → 退回默认 (0.382, 0.5, 0.618)
        # 选最深档位 0.618 → trigger=4476.4
        assert group.members[0].trigger_price == pytest.approx(4476.4, abs=0.1)

    def test_describe(self):
        d = FibPullbackEntryPolicy().describe()
        assert d["name"] == "fib_pullback"
        assert "fib_levels" in d["params_schema"]
