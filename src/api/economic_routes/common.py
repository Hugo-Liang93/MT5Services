from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from src.api.schemas import EconomicCalendarEventModel
from src.clients.economic_calendar import EconomicCalendarEvent


def normalize_datetime(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_model(event: EconomicCalendarEvent) -> EconomicCalendarEventModel:
    return EconomicCalendarEventModel(
        scheduled_at=event.scheduled_at.isoformat(),
        scheduled_at_local=event.scheduled_at_local.isoformat() if event.scheduled_at_local else None,
        local_timezone=event.local_timezone,
        scheduled_at_release=event.scheduled_at_release.isoformat() if event.scheduled_at_release else None,
        release_timezone=event.release_timezone,
        event_uid=event.event_uid,
        source=event.source,
        provider_event_id=event.provider_event_id,
        event_name=event.event_name,
        country=event.country,
        category=event.category,
        currency=event.currency,
        reference=event.reference,
        actual=event.actual,
        previous=event.previous,
        forecast=event.forecast,
        revised=event.revised,
        importance=event.importance,
        unit=event.unit,
        release_id=event.release_id,
        source_url=event.source_url,
        all_day=event.all_day,
        session_bucket=event.session_bucket,
        is_asia_session=event.is_asia_session,
        is_europe_session=event.is_europe_session,
        is_us_session=event.is_us_session,
        status=event.status,
        first_seen_at=event.first_seen_at.isoformat(),
        last_seen_at=event.last_seen_at.isoformat(),
        released_at=event.released_at.isoformat() if event.released_at else None,
        last_value_check_at=event.last_value_check_at.isoformat() if event.last_value_check_at else None,
        ingested_at=event.ingested_at.isoformat(),
        last_updated=event.last_updated.isoformat(),
    )
