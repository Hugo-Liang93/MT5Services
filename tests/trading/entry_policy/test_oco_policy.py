"""OcoEntryPolicy 单元测试 — A+B 组合 + sub_params 拆分。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.trading.entry_policy import (
    BarSnapshot,
    EntryIntent,
    EntryType,
    MarketSnapshot,
    OcoEntryPolicy,
    PatternType,
)


def _intent() -> EntryIntent:
    return EntryIntent(
        strategy_name="test",
        timeframe="M15",
        direction="buy",
        confidence=0.6,
        bar_time=datetime(2026, 4, 30, tzinfo=timezone.utc),
        pattern_type=PatternType.PIN_BULL,
    )


def _market() -> MarketSnapshot:
    bar = BarSnapshot(open=4515.0, high=4525.0, low=4485.0, close=4520.0)
    return MarketSnapshot(recent_bars=(bar,), atr_value=10.0, current_close=4520.0)


class TestOcoPolicy:
    def test_returns_two_member_group(self):
        policy = OcoEntryPolicy()
        group = policy.derive(_intent(), _market(), {})
        assert group.is_oco
        assert len(group.members) == 2

    def test_members_are_limit_pullback_and_stop_breakout(self):
        policy = OcoEntryPolicy()
        group = policy.derive(_intent(), _market(), {})
        member_ids = {m.member_id for m in group.members}
        assert member_ids == {"limit_pullback", "stop_breakout"}
        types = {m.entry_type for m in group.members}
        assert types == {EntryType.LIMIT, EntryType.STOP}

    def test_default_cancellation_policy_any_fill(self):
        policy = OcoEntryPolicy()
        group = policy.derive(_intent(), _market(), {})
        assert group.cancellation_policy == "any_fill"

    def test_cancellation_policy_override(self):
        policy = OcoEntryPolicy()
        group = policy.derive(
            _intent(),
            _market(),
            {"cancellation_policy": "all_or_none"},
        )
        assert group.cancellation_policy == "all_or_none"

    def test_invalid_cancellation_policy_falls_back_any_fill(self):
        policy = OcoEntryPolicy()
        group = policy.derive(
            _intent(),
            _market(),
            {"cancellation_policy": "garbage"},
        )
        assert group.cancellation_policy == "any_fill"

    def test_sub_params_split_by_prefix(self):
        """pullback_<key> 走 PullbackEntryPolicy, breakout_<key> 走 BreakoutEntryPolicy."""
        policy = OcoEntryPolicy()
        group = policy.derive(
            _intent(),
            _market(),
            {
                "pullback_branch_pin_bull_offset": 0.50,  # 大幅改 pullback
                "breakout_buffer_atr": 0.30,  # 大幅改 breakout
            },
        )
        members_by_id = {m.member_id: m for m in group.members}
        # Pullback: pin_bull anchor=4500, offset=0.50, trigger=4500 + 0.50*10=4505
        assert members_by_id["limit_pullback"].trigger_price == pytest.approx(4505.0)
        # Breakout: extreme=4525, buffer=0.30, trigger=4525 + 0.30*10=4528
        assert members_by_id["stop_breakout"].trigger_price == pytest.approx(4528.0)

    def test_metadata_includes_sub_branches(self):
        policy = OcoEntryPolicy()
        group = policy.derive(_intent(), _market(), {})
        assert group.metadata["policy_name"] == "oco_pullback_breakout"
        assert group.metadata["branch"] == "oco_a_plus_b"
        assert "pullback_meta" in group.metadata
        assert "breakout_meta" in group.metadata

    def test_unique_group_id_per_call(self):
        policy = OcoEntryPolicy()
        g1 = policy.derive(_intent(), _market(), {})
        g2 = policy.derive(_intent(), _market(), {})
        assert g1.group_id != g2.group_id

    def test_describe_lists_sub_policies(self):
        d = OcoEntryPolicy().describe()
        assert d["name"] == "oco_pullback_breakout"
        sub_names = {sub["name"] for sub in d["sub_policies"]}
        assert sub_names == {"pullback", "breakout"}
