from __future__ import annotations

from datetime import datetime, timezone

from src.calendar.economic_calendar.contracts import (
    SESSION_BUCKET_ALL_DAY,
    SESSION_BUCKET_ASIA_EUROPE_OVERLAP,
    SESSION_BUCKET_EUROPE_US_OVERLAP,
)
from src.clients.economic_calendar import EconomicCalendarEvent


def _make_event(scheduled_at: datetime, *, all_day: bool = False) -> EconomicCalendarEvent:
    return EconomicCalendarEvent(
        scheduled_at=scheduled_at,
        event_uid="test:event",
        source="test",
        provider_event_id="provider:1",
        event_name="Test Event",
        all_day=all_day,
    )


def test_all_day_event_uses_all_day_session_bucket() -> None:
    event = _make_event(datetime(2026, 4, 11, 0, 0, tzinfo=timezone.utc), all_day=True)

    event.enrich_time_context("Asia/Shanghai")

    assert event.session_bucket == SESSION_BUCKET_ALL_DAY
    assert event.is_asia_session is False
    assert event.is_europe_session is False
    assert event.is_us_session is False


def test_asia_europe_overlap_bucket_is_preserved() -> None:
    event = _make_event(datetime(2026, 4, 11, 7, 30, tzinfo=timezone.utc))

    event.enrich_time_context("Asia/Shanghai")

    assert event.session_bucket == SESSION_BUCKET_ASIA_EUROPE_OVERLAP
    assert event.is_asia_session is True
    assert event.is_europe_session is True
    assert event.is_us_session is False


def test_europe_us_overlap_bucket_is_preserved() -> None:
    event = _make_event(datetime(2026, 4, 11, 14, 0, tzinfo=timezone.utc))

    event.enrich_time_context("Asia/Shanghai")

    assert event.session_bucket == SESSION_BUCKET_EUROPE_US_OVERLAP
    assert event.is_asia_session is False
    assert event.is_europe_session is True
    assert event.is_us_session is True
