"""filtering/ 子包测试：BacktestFilterSimulator + BacktestFilterStats。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.backtesting.filtering.simulator import (
    BacktestFilterConfig,
    BacktestFilterSimulator,
    BacktestFilterStats,
)


class TestBacktestFilterStats:
    def test_initial_state(self) -> None:
        stats = BacktestFilterStats()
        assert stats.total_bars_evaluated == 0
        assert stats.total_bars_rejected == 0
        assert stats.pass_rate == 0.0

    def test_pass_rate_calculation(self) -> None:
        stats = BacktestFilterStats()
        stats.total_bars_evaluated = 100
        stats.total_bars_rejected = 25
        assert stats.pass_rate == 0.75

    def test_record_rejection_increments(self) -> None:
        stats = BacktestFilterStats()
        t = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        stats.record_rejection(t, "session:outside_hours")
        stats.record_rejection(t, "session:weekend")
        stats.record_rejection(t, "volatility:spike")
        assert stats.total_bars_rejected == 3
        assert stats.rejections_by_filter["session"] == 2
        assert stats.rejections_by_filter["volatility"] == 1

    def test_detail_records_capped(self) -> None:
        stats = BacktestFilterStats(max_detail_records=5)
        t = datetime(2025, 6, 1, tzinfo=timezone.utc)
        for i in range(10):
            stats.record_rejection(t, f"test:{i}")
        assert len(stats.rejection_details) == 5
        assert stats.total_bars_rejected == 10

    def test_to_dict(self) -> None:
        stats = BacktestFilterStats()
        stats.total_bars_evaluated = 50
        t = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        stats.record_rejection(t, "session:off_hours")
        d = stats.to_dict()
        assert d["total_bars_evaluated"] == 50
        assert d["total_bars_rejected"] == 1
        assert d["pass_rate"] == pytest.approx(0.98, abs=0.01)
        assert len(d["rejection_sample"]) == 1


class TestBacktestFilterSimulator:
    def test_disabled_always_passes(self) -> None:
        sim = BacktestFilterSimulator(BacktestFilterConfig(enabled=False))
        t = datetime(2025, 6, 1, 3, 0, tzinfo=timezone.utc)  # 亚盘凌晨
        allowed, reason = sim.should_evaluate("XAUUSD", bar_time=t)
        assert allowed is True
        assert reason == ""
        assert sim.stats.total_bars_evaluated == 1

    def test_session_filter_blocks_off_hours(self) -> None:
        sim = BacktestFilterSimulator(
            BacktestFilterConfig(
                enabled=True,
                session_filter_enabled=True,
                allowed_sessions="london,new_york",
                session_transition_enabled=False,
                volatility_filter_enabled=False,
            )
        )
        # UTC 03:00 周三 = 亚盘，不在 london/new_york 内
        t = datetime(2025, 6, 4, 3, 0, tzinfo=timezone.utc)  # Wednesday
        allowed, reason = sim.should_evaluate("XAUUSD", bar_time=t)
        assert allowed is False
        assert sim.stats.total_bars_rejected == 1

    def test_session_filter_passes_london(self) -> None:
        sim = BacktestFilterSimulator(
            BacktestFilterConfig(
                enabled=True,
                session_filter_enabled=True,
                allowed_sessions="london,new_york",
                session_transition_enabled=False,
                volatility_filter_enabled=False,
            )
        )
        # UTC 10:00 周三 = 伦敦盘
        t = datetime(2025, 6, 4, 10, 0, tzinfo=timezone.utc)  # Wednesday
        allowed, reason = sim.should_evaluate("XAUUSD", bar_time=t)
        assert allowed is True

    def test_all_filters_disabled_passes(self) -> None:
        sim = BacktestFilterSimulator(
            BacktestFilterConfig(
                enabled=True,
                session_filter_enabled=False,
                session_transition_enabled=False,
                volatility_filter_enabled=False,
                spread_filter_enabled=False,
                economic_filter_enabled=False,
            )
        )
        t = datetime(2025, 6, 4, 3, 0, tzinfo=timezone.utc)
        allowed, _ = sim.should_evaluate("XAUUSD", bar_time=t)
        assert allowed is True

    def test_stats_accumulate_across_calls(self) -> None:
        sim = BacktestFilterSimulator(
            BacktestFilterConfig(
                enabled=True,
                session_filter_enabled=True,
                allowed_sessions="london",
                session_transition_enabled=False,
                volatility_filter_enabled=False,
            )
        )
        # 伦敦盘内
        sim.should_evaluate("XAUUSD", bar_time=datetime(2025, 6, 4, 10, 0, tzinfo=timezone.utc))
        # 亚盘（被过滤）
        sim.should_evaluate("XAUUSD", bar_time=datetime(2025, 6, 4, 3, 0, tzinfo=timezone.utc))
        assert sim.stats.total_bars_evaluated == 2
        assert sim.stats.total_bars_rejected == 1

    def test_filter_chain_exposed(self) -> None:
        sim = BacktestFilterSimulator(
            BacktestFilterConfig(enabled=True, session_filter_enabled=True)
        )
        assert sim.filter_chain is not None

    def test_disabled_filter_chain_is_none(self) -> None:
        sim = BacktestFilterSimulator(BacktestFilterConfig(enabled=False))
        assert sim.filter_chain is None
