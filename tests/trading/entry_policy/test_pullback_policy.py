"""PullbackEntryPolicy 形态分支表 + 边界单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.trading.entry_policy import (
    BarSnapshot,
    EntryIntent,
    EntryType,
    MarketSnapshot,
    PatternType,
    PullbackEntryPolicy,
)


def _intent(
    direction: str = "buy",
    pattern: PatternType = PatternType.PIN_BULL,
    metadata: dict | None = None,
) -> EntryIntent:
    return EntryIntent(
        strategy_name="test",
        timeframe="M15",
        direction=direction,  # type: ignore[arg-type]
        confidence=0.6,
        bar_time=datetime(2026, 4, 30, tzinfo=timezone.utc),
        pattern_type=pattern,
        signal_metadata=metadata or {},
    )


def _market(
    *,
    bars: tuple[BarSnapshot, ...] | None = None,
    atr: float = 10.0,
    close: float = 4520.0,
) -> MarketSnapshot:
    if bars is None:
        bars = (BarSnapshot(open=4500.0, high=4525.0, low=4485.0, close=close),)
    return MarketSnapshot(recent_bars=bars, atr_value=atr, current_close=close)


class TestBranchTable:
    """每种 PatternType 对应的 anchor 与 branch label。"""

    def test_pin_bull_uses_lower_wick_50pct(self):
        bar = BarSnapshot(open=4515.0, high=4525.0, low=4485.0, close=4520.0)
        # body_low=4515, low=4485, wick_pct=0.5 → anchor=4485 + (4515-4485)*0.5 = 4500
        market = _market(bars=(bar,), atr=10.0, close=4520.0)
        policy = PullbackEntryPolicy()
        group = policy.derive(_intent(pattern=PatternType.PIN_BULL), market, {})
        assert group.is_single_member
        member = group.members[0]
        assert member.entry_type == EntryType.LIMIT
        # buy: trigger = anchor 4500 + 0.05 * 10 = 4500.5
        assert member.trigger_price == pytest.approx(4500.5)
        assert group.metadata["branch"] == "pin_bull"
        assert group.metadata["anchor"] == pytest.approx(4500.0)

    def test_pin_bear_uses_upper_wick_50pct(self):
        bar = BarSnapshot(open=4490.0, high=4525.0, low=4485.0, close=4495.0)
        # body_high=4495, high=4525 → anchor=4525 - (4525-4495)*0.5 = 4510
        market = _market(bars=(bar,), atr=10.0, close=4495.0)
        policy = PullbackEntryPolicy()
        group = policy.derive(
            _intent(direction="sell", pattern=PatternType.PIN_BEAR), market, {}
        )
        # sell: trigger = anchor 4510 - 0.05 * 10 = 4509.5
        assert group.members[0].trigger_price == pytest.approx(4509.5)
        assert group.metadata["branch"] == "pin_bear"

    def test_engulfing_bull_uses_prev_high(self):
        prev = BarSnapshot(open=4490.0, high=4505.0, low=4480.0, close=4485.0)
        cur = BarSnapshot(open=4485.0, high=4530.0, low=4480.0, close=4525.0)
        market = MarketSnapshot(
            recent_bars=(prev, cur), atr_value=10.0, current_close=4525.0
        )
        policy = PullbackEntryPolicy()
        group = policy.derive(_intent(pattern=PatternType.ENGULFING_BULL), market, {})
        # buy: anchor=prev.high=4505, trigger=4505 + 0.10 * 10 = 4506
        assert group.members[0].trigger_price == pytest.approx(4506.0)
        assert group.metadata["branch"] == "engulfing_bull"

    def test_engulfing_bear_uses_prev_low(self):
        prev = BarSnapshot(open=4520.0, high=4535.0, low=4515.0, close=4530.0)
        cur = BarSnapshot(open=4530.0, high=4535.0, low=4490.0, close=4495.0)
        market = MarketSnapshot(
            recent_bars=(prev, cur), atr_value=10.0, current_close=4495.0
        )
        policy = PullbackEntryPolicy()
        group = policy.derive(
            _intent(direction="sell", pattern=PatternType.ENGULFING_BEAR), market, {}
        )
        # sell: anchor=prev.low=4515, trigger=4515 - 0.10 * 10 = 4514
        assert group.members[0].trigger_price == pytest.approx(4514.0)

    def test_engulfing_falls_back_when_prev_missing(self):
        bar = BarSnapshot(open=4500.0, high=4520.0, low=4490.0, close=4515.0)
        market = MarketSnapshot(
            recent_bars=(bar,), atr_value=10.0, current_close=4515.0
        )
        policy = PullbackEntryPolicy()
        group = policy.derive(_intent(pattern=PatternType.ENGULFING_BULL), market, {})
        # fallback to current_close: anchor=4515, trigger=4515 + 0.10 * 10 = 4516
        assert group.members[0].trigger_price == pytest.approx(4516.0)

    def test_big_bar_bull_uses_body_50pct(self):
        bar = BarSnapshot(open=4500.0, high=4530.0, low=4495.0, close=4525.0)
        # body_low=4500, body_high=4525 → mid=4512.5
        market = _market(bars=(bar,), atr=10.0, close=4525.0)
        policy = PullbackEntryPolicy()
        group = policy.derive(_intent(pattern=PatternType.BIG_BAR_BULL), market, {})
        # trigger = 4512.5 + 0.10 * 10 = 4513.5
        assert group.members[0].trigger_price == pytest.approx(4513.5)
        assert group.metadata["branch"] == "big_bar_bull"

    def test_three_soldiers_uses_first_bar_close(self):
        b1 = BarSnapshot(open=4500.0, high=4505.0, low=4498.0, close=4503.0)
        b2 = BarSnapshot(open=4503.0, high=4510.0, low=4502.0, close=4509.0)
        b3 = BarSnapshot(open=4509.0, high=4520.0, low=4508.0, close=4518.0)
        market = MarketSnapshot(
            recent_bars=(b1, b2, b3), atr_value=10.0, current_close=4518.0
        )
        policy = PullbackEntryPolicy()
        group = policy.derive(_intent(pattern=PatternType.THREE_SOLDIERS), market, {})
        # anchor=b1.close=4503, trigger=4503 + 0.15 * 10 = 4504.5
        assert group.members[0].trigger_price == pytest.approx(4504.5)
        assert group.metadata["branch"] == "three_soldiers"

    def test_rejection_bull_uses_low_plus_buffer(self):
        bar = BarSnapshot(open=4500.0, high=4520.0, low=4480.0, close=4515.0)
        market = _market(bars=(bar,), atr=10.0, close=4515.0)
        policy = PullbackEntryPolicy()
        group = policy.derive(_intent(pattern=PatternType.REJECTION_BULL), market, {})
        # anchor = 4480 + 0.2 * 10 = 4482, trigger = 4482 + 0.20 * 10 = 4484
        assert group.members[0].trigger_price == pytest.approx(4484.0)

    def test_trendline_touch_uses_metadata_price(self):
        bar = BarSnapshot(open=4500.0, high=4520.0, low=4490.0, close=4515.0)
        market = _market(bars=(bar,), atr=10.0, close=4515.0)
        policy = PullbackEntryPolicy()
        intent = _intent(
            pattern=PatternType.TRENDLINE_TOUCH_BULL,
            metadata={"trendline_price": 4505.0},
        )
        group = policy.derive(intent, market, {})
        # anchor=4505 (from metadata), trigger=4505 + 0.10 * 10 = 4506
        assert group.members[0].trigger_price == pytest.approx(4506.0)

    def test_fallback_when_pattern_none(self):
        bar = BarSnapshot(open=4500.0, high=4520.0, low=4490.0, close=4515.0)
        market = _market(bars=(bar,), atr=10.0, close=4515.0)
        policy = PullbackEntryPolicy()
        group = policy.derive(_intent(pattern=PatternType.NONE), market, {})
        # fallback: anchor=current_close=4515, offset=0.30, trigger=4515 + 3 = 4518
        assert group.members[0].trigger_price == pytest.approx(4518.0)
        assert group.metadata["branch"] == "fallback"


class TestParamsOverride:
    def test_zone_atr_changes_entry_zone(self):
        bar = BarSnapshot(open=4515.0, high=4525.0, low=4485.0, close=4520.0)
        market = _market(bars=(bar,), atr=10.0, close=4520.0)
        policy = PullbackEntryPolicy()
        group = policy.derive(
            _intent(pattern=PatternType.PIN_BULL),
            market,
            {"zone_atr": 0.5},
        )
        member = group.members[0]
        # zone half = 0.5 * 10 = 5
        assert member.entry_high - member.entry_low == pytest.approx(10.0)
        assert group.metadata["zone_atr"] == 0.5

    def test_branch_offset_override(self):
        bar = BarSnapshot(open=4515.0, high=4525.0, low=4485.0, close=4520.0)
        market = _market(bars=(bar,), atr=10.0, close=4520.0)
        policy = PullbackEntryPolicy()
        group = policy.derive(
            _intent(pattern=PatternType.PIN_BULL),
            market,
            {"branch_pin_bull_offset": 0.20},
        )
        # anchor=4500, trigger=4500 + 0.20 * 10 = 4502
        assert group.members[0].trigger_price == pytest.approx(4502.0)
        assert group.metadata["offset_atr"] == 0.20

    def test_validity_bars_per_tf(self):
        bar = BarSnapshot(open=4515.0, high=4525.0, low=4485.0, close=4520.0)
        market = _market(bars=(bar,), atr=10.0, close=4520.0)
        policy = PullbackEntryPolicy()
        group = policy.derive(
            _intent(pattern=PatternType.PIN_BULL),
            market,
            {"validity_bars_M15": 3},
        )
        assert group.members[0].validity_bars == 3

    def test_invalid_param_falls_back_to_default(self):
        bar = BarSnapshot(open=4515.0, high=4525.0, low=4485.0, close=4520.0)
        market = _market(bars=(bar,), atr=10.0, close=4520.0)
        policy = PullbackEntryPolicy()
        group = policy.derive(
            _intent(pattern=PatternType.PIN_BULL),
            market,
            {"zone_atr": "not-a-number"},
        )
        # default 0.10
        member = group.members[0]
        assert member.entry_high - member.entry_low == pytest.approx(2.0)


class TestDescribe:
    def test_describe_includes_params_schema(self):
        d = PullbackEntryPolicy().describe()
        assert d["name"] == "pullback"
        assert "zone_atr" in d["params_schema"]
        assert "wick_pct" in d["params_schema"]
