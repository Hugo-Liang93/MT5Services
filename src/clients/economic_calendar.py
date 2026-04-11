from __future__ import annotations

import json
import logging
import random
import time as time_module
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from typing import Any, Dict, Iterable, List, Optional, runtime_checkable
from typing import Protocol
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.calendar.economic_calendar.contracts import (
    EVENT_STATUS_SCHEDULED,
    SESSION_BUCKET_ALL_DAY,
    SESSION_BUCKET_OFF_HOURS,
)
from src.config import EconomicConfig

logger = logging.getLogger(__name__)

COUNTRY_TIMEZONE_MAP = {
    "Australia": "Australia/Sydney",
    "Canada": "America/Toronto",
    "China": "Asia/Shanghai",
    "Euro Area": "Europe/Brussels",
    "European Union": "Europe/Brussels",
    "France": "Europe/Paris",
    "Germany": "Europe/Berlin",
    "Italy": "Europe/Rome",
    "Japan": "Asia/Tokyo",
    "New Zealand": "Pacific/Auckland",
    "Spain": "Europe/Madrid",
    "Switzerland": "Europe/Zurich",
    "United Kingdom": "Europe/London",
    "United States": "America/New_York",
}

_ASIA_SESSION = (22, 9)
_EUROPE_SESSION = (7, 16)
_US_SESSION = (13, 22)


class EconomicCalendarError(RuntimeError):
    pass


def _coerce_datetime(value: Any, *, all_day: bool = False) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    if value is None:
        raise ValueError("datetime value is required")

    text = str(value).strip()
    if not text:
        raise ValueError("datetime text is empty")

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if all_day:
        return datetime.combine(parsed.date(), time.min, tzinfo=timezone.utc)
    return parsed


def _as_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _importance_to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    mapping = {"low": 1, "medium": 2, "high": 3}
    if text in mapping:
        return mapping[text]
    try:
        return int(float(text))
    except ValueError:
        return None


def _event_in_date_range(value: datetime, start_date: date, end_date: date) -> bool:
    event_date = value.astimezone(timezone.utc).date()
    return start_date <= event_date <= end_date


def _load_zoneinfo(name: Optional[str], fallback: str = "UTC") -> ZoneInfo:
    candidate = (name or fallback or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        logger.warning("Unknown timezone '%s', falling back to %s", candidate, fallback)
        return ZoneInfo(fallback)


def _utc_hour_fraction(value: datetime) -> float:
    utc_value = value.astimezone(timezone.utc)
    return utc_value.hour + (utc_value.minute / 60.0) + (utc_value.second / 3600.0)


def _within_session(hour: float, start_hour: int, end_hour: int) -> bool:
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def _resolve_release_timezone(country: Optional[str], currency: Optional[str]) -> str:
    if country:
        mapped = COUNTRY_TIMEZONE_MAP.get(country.strip())
        if mapped:
            return mapped
    if currency == "USD":
        return "America/New_York"
    if currency == "EUR":
        return "Europe/Brussels"
    if currency == "GBP":
        return "Europe/London"
    if currency == "JPY":
        return "Asia/Tokyo"
    if currency == "CHF":
        return "Europe/Zurich"
    if currency == "CAD":
        return "America/Toronto"
    if currency == "AUD":
        return "Australia/Sydney"
    if currency == "NZD":
        return "Pacific/Auckland"
    if currency == "CNY":
        return "Asia/Shanghai"
    return "UTC"


def _classify_market_session(value: datetime) -> tuple[str, bool, bool, bool]:
    hour = _utc_hour_fraction(value)
    is_asia = _within_session(hour, *_ASIA_SESSION)
    is_europe = _within_session(hour, *_EUROPE_SESSION)
    is_us = _within_session(hour, *_US_SESSION)

    active = [name for name, enabled in (("asia", is_asia), ("europe", is_europe), ("us", is_us)) if enabled]
    if not active:
        return SESSION_BUCKET_OFF_HOURS, False, False, False
    if len(active) == 1:
        return active[0], is_asia, is_europe, is_us
    return "_".join(active) + "_overlap", is_asia, is_europe, is_us


@dataclass
class EconomicCalendarEvent:
    scheduled_at: datetime
    event_uid: str
    source: str
    provider_event_id: str
    event_name: str
    country: Optional[str] = None
    category: Optional[str] = None
    currency: Optional[str] = None
    reference: Optional[str] = None
    actual: Optional[str] = None
    previous: Optional[str] = None
    forecast: Optional[str] = None
    revised: Optional[str] = None
    importance: Optional[int] = None
    unit: Optional[str] = None
    release_id: Optional[str] = None
    source_url: Optional[str] = None
    all_day: bool = False
    scheduled_at_local: Optional[datetime] = None
    local_timezone: Optional[str] = None
    scheduled_at_release: Optional[datetime] = None
    release_timezone: Optional[str] = None
    session_bucket: str = SESSION_BUCKET_OFF_HOURS
    is_asia_session: bool = False
    is_europe_session: bool = False
    is_us_session: bool = False
    status: str = EVENT_STATUS_SCHEDULED
    first_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    released_at: Optional[datetime] = None
    last_value_check_at: Optional[datetime] = None
    raw_payload: Dict[str, Any] = field(default_factory=dict)
    ingested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def enrich_time_context(self, local_timezone_name: str) -> "EconomicCalendarEvent":
        local_tz = _load_zoneinfo(local_timezone_name, fallback="UTC")
        release_timezone_name = _resolve_release_timezone(self.country, self.currency)
        release_tz = _load_zoneinfo(release_timezone_name, fallback="UTC")

        self.local_timezone = local_tz.key
        self.release_timezone = release_tz.key
        if self.all_day:
            release_day = self.scheduled_at.date()
            midnight = datetime.combine(release_day, time.min)
            self.scheduled_at_local = midnight
            self.scheduled_at_release = midnight
            self.session_bucket = SESSION_BUCKET_ALL_DAY
            self.is_asia_session = False
            self.is_europe_session = False
            self.is_us_session = False
        else:
            session_bucket, is_asia, is_europe, is_us = _classify_market_session(self.scheduled_at)
            self.scheduled_at_local = self.scheduled_at.astimezone(local_tz).replace(tzinfo=None)
            self.scheduled_at_release = self.scheduled_at.astimezone(release_tz).replace(tzinfo=None)
            self.session_bucket = session_bucket
            self.is_asia_session = is_asia
            self.is_europe_session = is_europe
            self.is_us_session = is_us
        return self

    def to_row(self) -> tuple:
        return (
            self.scheduled_at,
            self.event_uid,
            self.source,
            self.provider_event_id,
            self.event_name,
            self.country,
            self.category,
            self.currency,
            self.reference,
            self.actual,
            self.previous,
            self.forecast,
            self.revised,
            self.importance,
            self.unit,
            self.release_id,
            self.source_url,
            self.all_day,
            self.scheduled_at_local,
            self.local_timezone,
            self.scheduled_at_release,
            self.release_timezone,
            self.session_bucket,
            self.is_asia_session,
            self.is_europe_session,
            self.is_us_session,
            self.status,
            self.first_seen_at,
            self.last_seen_at,
            self.released_at,
            self.last_value_check_at,
            self.raw_payload,
            self.ingested_at,
            self.last_updated,
        )

    def to_update_row(
        self,
        *,
        recorded_at: datetime,
        snapshot_reason: str,
        job_type: str,
    ) -> tuple:
        return (
            recorded_at,
            self.event_uid,
            self.scheduled_at,
            self.source,
            self.provider_event_id,
            self.event_name,
            self.country,
            self.currency,
            self.status,
            snapshot_reason,
            job_type,
            self.actual,
            self.previous,
            self.forecast,
            self.revised,
            self.importance,
            self.raw_payload,
        )

    @classmethod
    def from_db_row(cls, row: tuple) -> "EconomicCalendarEvent":
        return cls(
            scheduled_at=_coerce_datetime(row[0]),
            event_uid=row[1],
            source=row[2],
            provider_event_id=row[3],
            event_name=row[4],
            country=row[5],
            category=row[6],
            currency=row[7],
            reference=row[8],
            actual=row[9],
            previous=row[10],
            forecast=row[11],
            revised=row[12],
            importance=row[13],
            unit=row[14],
            release_id=row[15],
            source_url=row[16],
            all_day=bool(row[17]),
            scheduled_at_local=row[18],
            local_timezone=row[19],
            scheduled_at_release=row[20],
            release_timezone=row[21],
            session_bucket=row[22] or SESSION_BUCKET_OFF_HOURS,
            is_asia_session=bool(row[23]),
            is_europe_session=bool(row[24]),
            is_us_session=bool(row[25]),
            status=row[26] or EVENT_STATUS_SCHEDULED,
            first_seen_at=_coerce_datetime(row[27]),
            last_seen_at=_coerce_datetime(row[28]),
            released_at=_coerce_datetime(row[29]) if row[29] is not None else None,
            last_value_check_at=_coerce_datetime(row[30]) if row[30] is not None else None,
            raw_payload=row[31] or {},
            ingested_at=_coerce_datetime(row[32]),
            last_updated=_coerce_datetime(row[33]),
        )


@runtime_checkable
class EconomicCalendarProvider(Protocol):
    """经济日历数据源提供者协议。"""

    @property
    def name(self) -> str:
        """Provider 唯一标识（如 'tradingeconomics', 'fred', 'fmp'）。"""
        ...

    def fetch_events(
        self,
        start_date: date,
        end_date: date,
        countries: Optional[List[str]] = None,
    ) -> List[EconomicCalendarEvent]:
        """获取指定日期范围内的经济事件。"""
        ...

    def supports_release_watch(self) -> bool:
        """是否支持实时发布监控（高频轮询模式）。"""
        ...

    def is_configured(self) -> bool:
        """API key 等必要配置是否就绪。"""
        ...


class _BaseHttpClient:
    def __init__(self, settings: EconomicConfig):
        self.settings = settings

    @staticmethod
    def _request_headers() -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Connection": "close",
            "User-Agent": "MT5Services/1.0",
        }

    def _request_json(self, base_url: str, params: Dict[str, Any]) -> Dict[str, Any] | List[Any]:
        filtered = {key: value for key, value in params.items() if value not in (None, "", [])}
        url = f"{base_url}?{urlencode(filtered, doseq=True)}"
        attempts = max(1, int(self.settings.request_retries))
        timeout_seconds = max(1.0, float(self.settings.request_timeout_seconds))
        last_error: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            request = Request(url, headers=self._request_headers())
            try:
                with urlopen(request, timeout=timeout_seconds) as response:
                    payload = response.read().decode("utf-8")
                return json.loads(payload)
            except json.JSONDecodeError as exc:
                raise EconomicCalendarError(f"Invalid JSON response from {url}: {exc}") from exc
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                backoff = max(0.1, float(self.settings.retry_backoff_seconds)) * (2 ** (attempt - 1))
                jitter = random.uniform(0.0, max(0.0, float(self.settings.refresh_jitter_seconds)))
                sleep_seconds = backoff + jitter
                logger.warning(
                    "Economic calendar request failed (%s/%s) for %s, retrying in %.2fs: %s",
                    attempt,
                    attempts,
                    url,
                    sleep_seconds,
                    exc,
                )
                time_module.sleep(sleep_seconds)
        raise EconomicCalendarError(f"Request failed for {url}: {last_error}") from last_error


class TradingEconomicsCalendarClient(_BaseHttpClient):
    base_url = "https://api.tradingeconomics.com/calendar"

    @property
    def name(self) -> str:
        return "tradingeconomics"

    def supports_release_watch(self) -> bool:
        return True

    def is_configured(self) -> bool:
        return bool(self.settings.tradingeconomics_api_key)

    def fetch_events(
        self,
        start_date: date,
        end_date: date,
        countries: Optional[List[str]] = None,
    ) -> List[EconomicCalendarEvent]:
        api_key = self.settings.tradingeconomics_api_key
        if not api_key:
            return []

        path = self.base_url
        if countries:
            encoded_countries = quote(",".join(countries), safe=",")
            path = f"{path}/country/{encoded_countries}/{start_date.isoformat()}/{end_date.isoformat()}"
            response = self._request_json(path, {"c": api_key, "f": "json"})
        else:
            # TradingEconomics documents point-in-time range queries on country-scoped paths.
            # Without a country filter, fall back to snapshot mode and filter locally.
            response = self._request_json(path, {"c": api_key, "f": "json"})

        if not isinstance(response, list):
            raise EconomicCalendarError("Unexpected TradingEconomics response payload")

        events: List[EconomicCalendarEvent] = []
        for item in response:
            try:
                provider_event_id = _as_text(
                    item.get("CalendarId")
                    or item.get("EventID")
                    or item.get("Ticker")
                    or item.get("Event")
                )
                scheduled_at = _coerce_datetime(item.get("Date"))
                if not _event_in_date_range(scheduled_at, start_date, end_date):
                    continue
                event_name = _as_text(item.get("Event")) or "unknown_event"
                event_uid = f"tradingeconomics:{provider_event_id or event_name}"
                events.append(
                    EconomicCalendarEvent(
                        scheduled_at=scheduled_at,
                        event_uid=event_uid,
                        source="tradingeconomics",
                        provider_event_id=provider_event_id or event_uid,
                        event_name=event_name,
                        country=_as_text(item.get("Country")),
                        category=_as_text(item.get("Category")),
                        currency=_as_text(item.get("Currency")),
                        reference=_as_text(item.get("Reference") or item.get("ReferenceDate")),
                        actual=_as_text(item.get("Actual")),
                        previous=_as_text(item.get("Previous")),
                        forecast=_as_text(item.get("Forecast") or item.get("TEForecast")),
                        revised=_as_text(item.get("Revised")),
                        importance=_importance_to_int(item.get("Importance")),
                        unit=_as_text(item.get("Unit")),
                        release_id=_as_text(item.get("CalendarId")),
                        source_url=_as_text(item.get("URL")),
                        all_day=False,
                        raw_payload=item,
                    )
                )
            except Exception:
                logger.exception("Failed to normalize TradingEconomics event: %s", item)
        return events


class FredCalendarClient(_BaseHttpClient):
    releases_dates_url = "https://api.stlouisfed.org/fred/releases/dates"

    @property
    def name(self) -> str:
        return "fred"

    def supports_release_watch(self) -> bool:
        return False

    def is_configured(self) -> bool:
        return bool(self.settings.fred_api_key)

    def _should_include_release(self, release_id: str, release_name: str) -> bool:
        whitelist_ids = {item.strip() for item in self.settings.fred_release_whitelist_ids if item.strip()}
        whitelist_keywords = [item.strip().lower() for item in self.settings.fred_release_whitelist_keywords if item.strip()]
        blacklist_keywords = [item.strip().lower() for item in self.settings.fred_release_blacklist_keywords if item.strip()]

        normalized_name = release_name.lower()
        if blacklist_keywords and any(keyword in normalized_name for keyword in blacklist_keywords):
            return False

        # Empty whitelist means no allow-list filtering.
        if not whitelist_ids and not whitelist_keywords:
            return True

        if whitelist_ids and release_id in whitelist_ids:
            return True
        if whitelist_keywords and any(keyword in normalized_name for keyword in whitelist_keywords):
            return True
        return False

    def fetch_events(
        self,
        start_date: date,
        end_date: date,
        countries: Optional[List[str]] = None,
    ) -> List[EconomicCalendarEvent]:
        api_key = self.settings.fred_api_key
        if not api_key:
            return []

        response = self._request_json(
            self.releases_dates_url,
            {
                "api_key": api_key,
                "file_type": "json",
                "realtime_start": start_date.isoformat(),
                "realtime_end": end_date.isoformat(),
                "include_release_dates_with_no_data": "true",
            },
        )
        release_dates = response.get("release_dates", []) if isinstance(response, dict) else []
        events: List[EconomicCalendarEvent] = []
        for item in release_dates:
            try:
                release_id = _as_text(item.get("release_id")) or "unknown_release"
                release_name = _as_text(item.get("release_name")) or "FRED Release"
                if not self._should_include_release(release_id, release_name):
                    continue
                scheduled_at = _coerce_datetime(item.get("date"), all_day=True)
                event_uid = f"fred:{release_id}:{scheduled_at.date().isoformat()}"
                events.append(
                    EconomicCalendarEvent(
                        scheduled_at=scheduled_at,
                        event_uid=event_uid,
                        source="fred",
                        provider_event_id=release_id,
                        event_name=release_name,
                        country="United States",
                        category="release",
                        currency="USD",
                        reference=_as_text(item.get("date")),
                        release_id=release_id,
                        source_url=f"https://fred.stlouisfed.org/release?rid={release_id}",
                        all_day=True,
                        raw_payload=item,
                    )
                )
            except Exception:
                logger.exception("Failed to normalize FRED release date: %s", item)
        return events
