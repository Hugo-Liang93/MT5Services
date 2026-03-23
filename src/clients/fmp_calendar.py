"""FMP (Financial Modeling Prep) 经济日历客户端。

API 文档：https://site.financialmodelingprep.com/developer/docs
免费层：250 请求/天
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date
from typing import List, Optional

from src.clients.economic_calendar import (
    EconomicCalendarEvent,
    _BaseHttpClient,
    _as_text,
    _coerce_datetime,
    _event_in_date_range,
    _importance_to_int,
)

logger = logging.getLogger(__name__)


class FmpCalendarClient(_BaseHttpClient):
    base_url = "https://financialmodelingprep.com/api/v3/economic_calendar"

    @property
    def name(self) -> str:
        return "fmp"

    def supports_release_watch(self) -> bool:
        return True

    def is_configured(self) -> bool:
        return bool(self.settings.fmp_api_key)

    def fetch_events(
        self,
        start_date: date,
        end_date: date,
        countries: Optional[List[str]] = None,
    ) -> List[EconomicCalendarEvent]:
        api_key = self.settings.fmp_api_key
        if not api_key:
            return []

        response = self._request_json(
            self.base_url,
            {
                "from": start_date.isoformat(),
                "to": end_date.isoformat(),
                "apikey": api_key,
            },
        )

        if not isinstance(response, list):
            return []

        country_filter = (
            {c.strip().lower() for c in countries} if countries else None
        )

        events: List[EconomicCalendarEvent] = []
        for item in response:
            try:
                country = _as_text(item.get("country"))
                if country_filter and country and country.strip().lower() not in country_filter:
                    continue

                date_str = item.get("date")
                if not date_str:
                    continue
                scheduled_at = _coerce_datetime(date_str)
                if not _event_in_date_range(scheduled_at, start_date, end_date):
                    continue

                event_name = _as_text(item.get("event")) or "unknown_event"
                # FMP 没有唯一 ID，用 event + date + hash 作为标识
                raw_id = f"{event_name}:{country or ''}:{date_str}"
                short_hash = hashlib.md5(raw_id.encode()).hexdigest()[:8]
                provider_event_id = f"{event_name}:{date_str}:{short_hash}"
                event_uid = f"fmp:{provider_event_id}"

                # impact 字段：Low/Medium/High → 1/2/3
                importance = _importance_to_int(item.get("impact"))

                events.append(
                    EconomicCalendarEvent(
                        scheduled_at=scheduled_at,
                        event_uid=event_uid,
                        source="fmp",
                        provider_event_id=provider_event_id,
                        event_name=event_name,
                        country=country,
                        category=_as_text(item.get("category")),
                        currency=_as_text(item.get("currency")),
                        actual=_as_text(item.get("actual")),
                        previous=_as_text(item.get("previous")),
                        forecast=_as_text(item.get("estimate")),
                        revised=_as_text(item.get("change")),
                        importance=importance,
                        unit=_as_text(item.get("unit")),
                        all_day=False,
                        raw_payload=item,
                    )
                )
            except Exception:
                logger.exception("Failed to normalize FMP event: %s", item)
        return events
