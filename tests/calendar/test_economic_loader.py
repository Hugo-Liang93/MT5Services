"""src/calendar/economic_loader.py 单元测试。

守护 P4 迁移契约：
  - SimpleEvent 字段与旧 _SimpleEvent 完全一致
  - load_economic_events_window 行为与旧 load_backtest_economic_events 一致
    （通过 fake repo 验证：缓冲扩窗、currency/importance 过滤、时区归一）
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from src.calendar.economic_loader import SimpleEvent, load_economic_events_window


class _FakeRepo:
    """模拟 EconomicCalendarRepository.fetch_economic_calendar 返回。"""

    def __init__(self, rows: List[tuple]) -> None:
        self.rows = rows
        self.last_call: dict = {}

    def fetch_economic_calendar(
        self,
        start_time: datetime,
        end_time: datetime,
        limit: int = 5000,
        currencies: Optional[List[str]] = None,
        importance_min: int = 2,
    ) -> List[tuple]:
        self.last_call = {
            "start_time": start_time,
            "end_time": end_time,
            "limit": limit,
            "currencies": currencies,
            "importance_min": importance_min,
        }
        return self.rows


def _make_row(
    scheduled_at: datetime,
    event_name: str = "NFP",
    importance: int = 3,
    currency: str = "USD",
) -> tuple:
    """构造与 repo.fetch_economic_calendar 返回 schema 一致的 row。"""
    # Index 必须与 economic_loader._SimpleEvent 构造保持一致：
    # 0=scheduled_at, 1=event_uid, 2=source, 4=event_name, 5=country,
    # 6=category, 7=currency, 13=importance, 22=session_bucket, 26=status
    row = [None] * 27
    row[0] = scheduled_at
    row[1] = f"uid:{event_name}"
    row[2] = "test_source"
    row[4] = event_name
    row[5] = "United States"
    row[6] = "labor"
    row[7] = currency
    row[13] = importance
    row[22] = "new_york"
    row[26] = "released"
    return tuple(row)


class TestSimpleEventDataclass:
    def test_fields_match_legacy_contract(self) -> None:
        """SimpleEvent 字段签名必须与旧 _SimpleEvent 一致（防止 TradeGuard 失配）。"""
        ev = SimpleEvent(
            event_uid="uid",
            event_name="NFP",
            source="src",
            country="US",
            currency="USD",
            importance=3,
            scheduled_at=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
        )
        # 字段完整覆盖
        assert ev.event_uid == "uid"
        assert ev.event_name == "NFP"
        assert ev.source == "src"
        assert ev.country == "US"
        assert ev.currency == "USD"
        assert ev.importance == 3
        assert ev.status == "released"  # 默认值
        assert ev.session_bucket == ""  # 默认值
        assert ev.category == ""  # 默认值
        assert ev.scheduled_at_local is None
        assert ev.scheduled_at_release is None


class TestLoadEconomicEventsWindow:
    def test_applies_buffer_hours_to_repo_query(self) -> None:
        """buffer_hours 必须同时扩展 start/end 边界，以捕获边界事件。"""
        start = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
        end = datetime(2026, 3, 15, 14, 0, tzinfo=timezone.utc)
        repo = _FakeRepo([])

        load_economic_events_window(
            economic_repo=repo,
            start_time=start,
            end_time=end,
            buffer_hours=6,
        )

        assert repo.last_call["start_time"] == start - timedelta(hours=6)
        assert repo.last_call["end_time"] == end + timedelta(hours=6)
        assert repo.last_call["limit"] == 5000

    def test_passes_currency_and_importance_filters(self) -> None:
        repo = _FakeRepo([])
        load_economic_events_window(
            economic_repo=repo,
            start_time=datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, 0, 0, tzinfo=timezone.utc),
            currencies=["USD", "EUR"],
            importance_min=3,
        )
        assert repo.last_call["currencies"] == ["USD", "EUR"]
        assert repo.last_call["importance_min"] == 3

    def test_rows_converted_to_simple_events(self) -> None:
        scheduled = datetime(2026, 3, 15, 12, 30, tzinfo=timezone.utc)
        repo = _FakeRepo(
            [
                _make_row(scheduled, event_name="NFP", importance=3, currency="USD"),
                _make_row(
                    scheduled + timedelta(hours=1),
                    event_name="CPI",
                    importance=2,
                    currency="EUR",
                ),
            ]
        )

        events = load_economic_events_window(
            economic_repo=repo,
            start_time=scheduled - timedelta(hours=1),
            end_time=scheduled + timedelta(hours=2),
        )

        assert len(events) == 2
        assert events[0].event_name == "NFP"
        assert events[0].importance == 3
        assert events[0].currency == "USD"
        assert events[0].scheduled_at.tzinfo is timezone.utc
        assert events[1].event_name == "CPI"

    def test_naive_datetime_rows_normalized_to_utc(self) -> None:
        """DB 返回无时区的 datetime 时，loader 应补齐 UTC。"""
        naive = datetime(2026, 3, 15, 12, 0)  # tzinfo=None
        repo = _FakeRepo([_make_row(naive, event_name="FOMC")])

        events = load_economic_events_window(
            economic_repo=repo,
            start_time=datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, 0, 0, tzinfo=timezone.utc),
        )
        assert len(events) == 1
        assert events[0].scheduled_at.tzinfo is timezone.utc

    def test_rows_with_null_scheduled_at_skipped(self) -> None:
        repo = _FakeRepo(
            [
                _make_row(datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)),
                _make_row(None),  # type: ignore[arg-type]
            ]
        )
        events = load_economic_events_window(
            economic_repo=repo,
            start_time=datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, 0, 0, tzinfo=timezone.utc),
        )
        assert len(events) == 1
