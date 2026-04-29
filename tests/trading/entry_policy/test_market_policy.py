"""MarketEntryPolicy 单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from src.trading.entry_policy import (
    BarSnapshot,
    EntryIntent,
    EntryType,
    MarketEntryPolicy,
    MarketSnapshot,
    PatternType,
)


def _make_intent(direction: str = "buy") -> EntryIntent:
    return EntryIntent(
        strategy_name="structured_market",
        timeframe="M15",
        direction=direction,  # type: ignore[arg-type]
        confidence=0.6,
        bar_time=datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc),
        pattern_type=PatternType.NONE,
    )


def _make_market(close: float = 4500.0) -> MarketSnapshot:
    bars = (
        BarSnapshot.from_mapping(
            {"open": 4490.0, "high": 4510.0, "low": 4485.0, "close": close}
        ),
    )
    return MarketSnapshot(recent_bars=bars, atr_value=10.0, current_close=close)


class TestMarketEntryPolicy:
    def test_returns_single_member_market(self):
        policy = MarketEntryPolicy()
        group = policy.derive(_make_intent(), _make_market(4500.0), {})

        assert group.is_single_member is True
        assert group.is_oco is False
        assert group.all_market() is True
        assert len(group.members) == 1
        assert group.members[0].entry_type == EntryType.MARKET
        assert group.members[0].member_id == "market"
        assert group.members[0].trigger_price == 4500.0

    def test_trigger_price_uses_current_close(self):
        policy = MarketEntryPolicy()
        group = policy.derive(_make_intent(), _make_market(4575.5), {})
        assert group.members[0].trigger_price == 4575.5

    def test_metadata_records_policy_and_pattern(self):
        policy = MarketEntryPolicy()
        intent = EntryIntent(
            strategy_name="structured_x",
            timeframe="H1",
            direction="sell",
            confidence=0.7,
            bar_time=datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc),
            pattern_type=PatternType.ENGULFING_BEAR,
        )
        group = policy.derive(intent, _make_market(), {})
        assert group.metadata["policy_name"] == "market"
        assert group.metadata["branch"] == "market"
        assert group.metadata["pattern_type"] == "engulfing_bear"

    def test_describe_returns_dict(self):
        d = MarketEntryPolicy().describe()
        assert d["name"] == "market"
        assert d["kind"] == "single_member_market"
        assert "params_schema" in d
        assert "description" in d

    def test_params_ignored(self):
        """market policy 不读 params；传任何 dict 都不应出错。"""
        policy = MarketEntryPolicy()
        group = policy.derive(_make_intent(), _make_market(), {"any": "value"})
        assert group.is_single_member is True
