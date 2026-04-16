"""tests/research/features/test_session_event_provider.py

SessionEventFeatureProvider 单元测试。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from src.research.features.protocol import FeatureRole
from src.research.features.session_event import SessionEventProvider


# ---------------------------------------------------------------------------
# Mock DataMatrix helpers
# ---------------------------------------------------------------------------


def _make_matrix(
    bar_times: List[datetime],
    high_impact_event_times: Tuple[datetime, ...] = (),
    timeframe: str = "H1",
) -> Any:
    """构造最小化 DataMatrix mock（仅 session_event 所需字段）。"""
    m = MagicMock()
    m.n_bars = len(bar_times)
    m.bar_times = bar_times
    m.high_impact_event_times = high_impact_event_times
    m.timeframe = timeframe
    return m


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """创建 UTC datetime 便利函数。"""
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Provider 基本属性
# ---------------------------------------------------------------------------


class TestProviderMetadata:
    def test_name(self) -> None:
        p = SessionEventProvider()
        assert p.name == "session_event"

    def test_feature_count(self) -> None:
        p = SessionEventProvider()
        assert p.feature_count == 7

    def test_required_columns_empty(self) -> None:
        p = SessionEventProvider()
        assert p.required_columns() == []

    def test_no_extra_data_required(self) -> None:
        p = SessionEventProvider()
        assert p.required_extra_data() is None

    def test_role_mapping_all_when(self) -> None:
        p = SessionEventProvider()
        rm = p.role_mapping()
        assert len(rm) == 7
        for role in rm.values():
            assert role == FeatureRole.WHEN


# ---------------------------------------------------------------------------
# session_phase
# ---------------------------------------------------------------------------


class TestSessionPhase:
    def test_asia_hour_3(self) -> None:
        """小时 3 → 亚盘 0。"""
        times = [_utc(2024, 1, 2, 3)]
        m = _make_matrix(times)
        p = SessionEventProvider()
        result = p.compute(m)
        vals = result[("session_event", "session_phase")]
        assert vals[0] == pytest.approx(0.0)

    def test_london_hour_8(self) -> None:
        """小时 8 → 欧盘 1。"""
        times = [_utc(2024, 1, 2, 8)]
        m = _make_matrix(times)
        p = SessionEventProvider()
        result = p.compute(m)
        vals = result[("session_event", "session_phase")]
        assert vals[0] == pytest.approx(1.0)

    def test_ny_hour_15(self) -> None:
        """小时 15 → 美盘 2。"""
        times = [_utc(2024, 1, 2, 15)]
        m = _make_matrix(times)
        p = SessionEventProvider()
        result = p.compute(m)
        vals = result[("session_event", "session_phase")]
        assert vals[0] == pytest.approx(2.0)

    def test_close_hour_22(self) -> None:
        """小时 22 → 尾盘 3。"""
        times = [_utc(2024, 1, 2, 22)]
        m = _make_matrix(times)
        p = SessionEventProvider()
        result = p.compute(m)
        vals = result[("session_event", "session_phase")]
        assert vals[0] == pytest.approx(3.0)

    def test_boundary_hour_7(self) -> None:
        """小时 7 → 欧盘开始 1。"""
        times = [_utc(2024, 1, 2, 7)]
        m = _make_matrix(times)
        p = SessionEventProvider()
        result = p.compute(m)
        vals = result[("session_event", "session_phase")]
        assert vals[0] == pytest.approx(1.0)

    def test_boundary_hour_21(self) -> None:
        """小时 21 → 尾盘 3。"""
        times = [_utc(2024, 1, 2, 21)]
        m = _make_matrix(times)
        p = SessionEventProvider()
        result = p.compute(m)
        vals = result[("session_event", "session_phase")]
        assert vals[0] == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# london_session
# ---------------------------------------------------------------------------


class TestLondonSession:
    def test_in_london(self) -> None:
        """小时 10 在伦敦盘内 → 1.0。"""
        times = [_utc(2024, 1, 2, 10)]
        m = _make_matrix(times)
        p = SessionEventProvider()
        result = p.compute(m)
        vals = result[("session_event", "london_session")]
        assert vals[0] == pytest.approx(1.0)

    def test_outside_london(self) -> None:
        """小时 20 在伦敦盘外 → 0.0。"""
        times = [_utc(2024, 1, 2, 20)]
        m = _make_matrix(times)
        p = SessionEventProvider()
        result = p.compute(m)
        vals = result[("session_event", "london_session")]
        assert vals[0] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# ny_session
# ---------------------------------------------------------------------------


class TestNySession:
    def test_in_ny(self) -> None:
        """小时 14 在纽约盘内 → 1.0。"""
        times = [_utc(2024, 1, 2, 14)]
        m = _make_matrix(times)
        p = SessionEventProvider()
        result = p.compute(m)
        vals = result[("session_event", "ny_session")]
        assert vals[0] == pytest.approx(1.0)

    def test_outside_ny(self) -> None:
        """小时 6 在纽约盘外 → 0.0。"""
        times = [_utc(2024, 1, 2, 6)]
        m = _make_matrix(times)
        p = SessionEventProvider()
        result = p.compute(m)
        vals = result[("session_event", "ny_session")]
        assert vals[0] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# day_progress
# ---------------------------------------------------------------------------


class TestDayProgress:
    def test_midnight(self) -> None:
        """午夜 00:00:00 → 0.0。"""
        times = [_utc(2024, 1, 2, 0, 0)]
        m = _make_matrix(times)
        p = SessionEventProvider()
        result = p.compute(m)
        vals = result[("session_event", "day_progress")]
        assert vals[0] == pytest.approx(0.0)

    def test_noon(self) -> None:
        """正午 12:00:00 → 0.5。"""
        times = [_utc(2024, 1, 2, 12, 0)]
        m = _make_matrix(times)
        p = SessionEventProvider()
        result = p.compute(m)
        vals = result[("session_event", "day_progress")]
        assert vals[0] == pytest.approx(0.5)

    def test_quarter_day(self) -> None:
        """06:00:00 → 0.25。"""
        times = [_utc(2024, 1, 2, 6, 0)]
        m = _make_matrix(times)
        p = SessionEventProvider()
        result = p.compute(m)
        vals = result[("session_event", "day_progress")]
        assert vals[0] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# in_news_window
# ---------------------------------------------------------------------------


class TestInNewsWindow:
    def test_bar_within_30min_before_event(self) -> None:
        """bar 在事件前 15min (09:45)，事件在 10:00 → 1.0（在窗口内）。"""
        event_time = _utc(2024, 1, 2, 10, 0)
        bar_time = _utc(2024, 1, 2, 9, 45)
        m = _make_matrix([bar_time], high_impact_event_times=(event_time,))
        p = SessionEventProvider()
        result = p.compute(m)
        vals = result[("session_event", "in_news_window")]
        assert vals[0] == pytest.approx(1.0)

    def test_bar_outside_window(self) -> None:
        """bar 在 12:00，事件在 10:00，距离 2h → 0.0（窗口外）。"""
        event_time = _utc(2024, 1, 2, 10, 0)
        bar_time = _utc(2024, 1, 2, 12, 0)
        m = _make_matrix([bar_time], high_impact_event_times=(event_time,))
        p = SessionEventProvider()
        result = p.compute(m)
        vals = result[("session_event", "in_news_window")]
        assert vals[0] == pytest.approx(0.0)

    def test_no_events(self) -> None:
        """无事件时所有 bar → 0.0。"""
        times = [_utc(2024, 1, 2, 10, 0), _utc(2024, 1, 2, 11, 0)]
        m = _make_matrix(times, high_impact_event_times=())
        p = SessionEventProvider()
        result = p.compute(m)
        vals = result[("session_event", "in_news_window")]
        assert all(v == pytest.approx(0.0) for v in vals)

    def test_bar_at_30min_boundary(self) -> None:
        """bar 与事件恰好相差 30min → 1.0（边界等于包含）。"""
        event_time = _utc(2024, 1, 2, 10, 0)
        bar_time = _utc(2024, 1, 2, 9, 30)
        m = _make_matrix([bar_time], high_impact_event_times=(event_time,))
        p = SessionEventProvider()
        result = p.compute(m)
        vals = result[("session_event", "in_news_window")]
        assert vals[0] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# bars_to_next / bars_since_last
# ---------------------------------------------------------------------------


class TestBarsToAndSinceEvent:
    def test_bars_to_next_no_events(self) -> None:
        """无事件 → None。"""
        times = [_utc(2024, 1, 2, 9, 0)]
        m = _make_matrix(times, high_impact_event_times=(), timeframe="H1")
        p = SessionEventProvider()
        result = p.compute(m)
        vals = result[("session_event", "bars_to_next_high_impact_event")]
        assert vals[0] is None

    def test_bars_since_last_no_events(self) -> None:
        """无事件 → None。"""
        times = [_utc(2024, 1, 2, 9, 0)]
        m = _make_matrix(times, high_impact_event_times=(), timeframe="H1")
        p = SessionEventProvider()
        result = p.compute(m)
        vals = result[("session_event", "bars_since_last_high_impact_event")]
        assert vals[0] is None

    def test_bars_to_next_h1(self) -> None:
        """H1 TF，bar 在 09:00，事件在 11:00 → delta = 2 bars。"""
        bar_time = _utc(2024, 1, 2, 9, 0)
        event_time = _utc(2024, 1, 2, 11, 0)
        m = _make_matrix([bar_time], high_impact_event_times=(event_time,), timeframe="H1")
        p = SessionEventProvider()
        result = p.compute(m)
        vals = result[("session_event", "bars_to_next_high_impact_event")]
        assert vals[0] == pytest.approx(2.0)

    def test_bars_since_last_h1(self) -> None:
        """H1 TF，bar 在 13:00，事件在 11:00 → delta = 2 bars。"""
        bar_time = _utc(2024, 1, 2, 13, 0)
        event_time = _utc(2024, 1, 2, 11, 0)
        m = _make_matrix([bar_time], high_impact_event_times=(event_time,), timeframe="H1")
        p = SessionEventProvider()
        result = p.compute(m)
        vals = result[("session_event", "bars_since_last_high_impact_event")]
        assert vals[0] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# compute() 返回结构
# ---------------------------------------------------------------------------


class TestComputeOutputStructure:
    def test_output_keys(self) -> None:
        """compute() 的返回字典包含所有 7 个预期的键。"""
        times = [_utc(2024, 1, 2, 9, 0)] * 5
        m = _make_matrix(times)
        p = SessionEventProvider()
        result = p.compute(m)
        expected_fields = [
            "session_phase",
            "london_session",
            "ny_session",
            "day_progress",
            "bars_to_next_high_impact_event",
            "bars_since_last_high_impact_event",
            "in_news_window",
        ]
        for field in expected_fields:
            assert ("session_event", field) in result, f"缺少键: {field}"

    def test_output_length_matches_n_bars(self) -> None:
        """每列长度等于 n_bars。"""
        times = [_utc(2024, 1, 2, 9, i) for i in range(10)]
        m = _make_matrix(times)
        p = SessionEventProvider()
        result = p.compute(m)
        for key, vals in result.items():
            assert len(vals) == 10, f"{key} 长度不匹配"

    def test_empty_matrix(self) -> None:
        """n_bars=0 时返回空列表。"""
        m = _make_matrix([])
        p = SessionEventProvider()
        result = p.compute(m)
        for vals in result.values():
            assert vals == []
