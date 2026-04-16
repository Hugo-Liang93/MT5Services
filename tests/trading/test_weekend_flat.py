"""周末强平契约测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.trading.positions.exit_rules import WeekendFlatPolicy, check_weekend_flat


def _t(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


class TestPolicyContract:
    def test_disabled_valid(self) -> None:
        p = WeekendFlatPolicy(enabled=False)
        assert not p.enabled

    def test_weekday_bounds(self) -> None:
        with pytest.raises(ValueError):
            WeekendFlatPolicy(enabled=True, weekday=7)
        with pytest.raises(ValueError):
            WeekendFlatPolicy(enabled=True, weekday=-1)

    def test_hour_bounds(self) -> None:
        with pytest.raises(ValueError):
            WeekendFlatPolicy(enabled=True, hour_utc=24)

    def test_minute_bounds(self) -> None:
        with pytest.raises(ValueError):
            WeekendFlatPolicy(enabled=True, minute_utc=60)


class TestCheckWeekendFlat:
    def test_disabled_never_triggers(self) -> None:
        p = WeekendFlatPolicy(enabled=False)
        # 周五 21:00 UTC
        assert not check_weekend_flat(_t(2026, 1, 2, 21), p, None)

    def test_wrong_weekday_no_trigger(self) -> None:
        # 2026-01-01 是 Thursday (weekday=3)
        p = WeekendFlatPolicy(enabled=True, weekday=4, hour_utc=20)
        assert not check_weekend_flat(_t(2026, 1, 1, 21), p, None)

    def test_before_trigger_hour(self) -> None:
        # 2026-01-02 是 Friday (weekday=4)
        p = WeekendFlatPolicy(enabled=True, weekday=4, hour_utc=20)
        assert not check_weekend_flat(_t(2026, 1, 2, 19, 59), p, None)

    def test_at_trigger_hour(self) -> None:
        p = WeekendFlatPolicy(enabled=True, weekday=4, hour_utc=20, minute_utc=0)
        assert check_weekend_flat(_t(2026, 1, 2, 20, 0), p, None)

    def test_after_trigger_hour(self) -> None:
        p = WeekendFlatPolicy(enabled=True, weekday=4, hour_utc=20)
        assert check_weekend_flat(_t(2026, 1, 2, 23, 30), p, None)

    def test_same_day_deduplication(self) -> None:
        p = WeekendFlatPolicy(enabled=True, weekday=4, hour_utc=20)
        day_key = "2026-01-02"
        # 已在今日触发过 → 不重复
        assert not check_weekend_flat(_t(2026, 1, 2, 21), p, day_key)

    def test_next_week_resets(self) -> None:
        p = WeekendFlatPolicy(enabled=True, weekday=4, hour_utc=20)
        # 上周五触发过
        last = "2025-12-26"
        # 这周五应再触发
        assert check_weekend_flat(_t(2026, 1, 2, 21), p, last)
