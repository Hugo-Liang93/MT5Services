from __future__ import annotations

import logging
from datetime import date, datetime, time as dt_time, timedelta, timezone
from threading import Event, Lock, RLock, Thread
from typing import Any, Dict, List, Optional

from src.clients.economic_calendar import (
    EconomicCalendarError,
    EconomicCalendarEvent,
    FredCalendarClient,
    TradingEconomicsCalendarClient,
)
from src.config import EconomicConfig, get_economic_config
from src.calendar.economic_calendar.calendar_sync import (
    ensure_worker_running,
    fetch_existing_by_uid,
    fetch_job_events,
    job_summary,
    persist_job_state,
    refresh_service,
    restore_job_state,
    run_job,
    run_scheduler,
    safe_run_job,
    schedule_next_run,
    start_service,
    startup_schedule_time,
    stop_service,
    store_with_backpressure_control,
    update_job_state,
    write_events,
)
from src.calendar.economic_calendar.observability import (
    get_updates as get_update_rows,
    stats as build_stats,
)
from src.calendar.economic_calendar.trade_guard import (
    build_window,
    compute_window_bounds,
    get_merged_risk_windows,
    get_risk_windows,
    get_trade_guard,
    infer_symbol_context,
    merge_windows,
    normalize_symbol,
    window_to_datetimes,
)
from src.persistence.db import TimescaleWriter
from src.persistence.storage_writer import StorageWriter

logger = logging.getLogger(__name__)

_JOB_LABELS = {
    "calendar_sync": "calendar_sync",
    "near_term_sync": "near_term_sync",
    "release_watch": "release_watch",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _start_of_day(value: date) -> datetime:
    return datetime.combine(value, dt_time.min, tzinfo=timezone.utc)


def _end_of_day(value: date) -> datetime:
    return datetime.combine(value, dt_time.max, tzinfo=timezone.utc)


class EconomicCalendarService:
    def __init__(
        self,
        db_writer: TimescaleWriter,
        settings: Optional[EconomicConfig] = None,
        storage_writer: Optional[StorageWriter] = None,
    ):
        self.db = db_writer
        self.settings = settings or get_economic_config()
        self.storage_writer = storage_writer
        self._lock = RLock()
        self._te_client = TradingEconomicsCalendarClient(self.settings)
        self._fred_client = FredCalendarClient(self.settings)
        self._last_refresh_at: Optional[datetime] = None
        self._last_refresh_error: Optional[str] = None
        self._last_refresh_summary: Optional[Dict[str, Any]] = None
        self._stop_event = Event()
        self._worker: Optional[Thread] = None
        self._refresh_lock = Lock()
        self._refresh_in_progress = False
        self._last_refresh_started_at: Optional[datetime] = None
        self._last_refresh_completed_at: Optional[datetime] = None
        self._last_refresh_duration_ms: Optional[int] = None
        self._consecutive_failures = 0
        self._next_run_at: Dict[str, Optional[datetime]] = {name: None for name in _JOB_LABELS}
        self._job_state: Dict[str, Dict[str, Any]] = {
            name: {
                "enabled": self._job_interval(name) > 0,
                "last_started_at": None,
                "last_completed_at": None,
                "last_error": None,
                "last_status": None,
                "last_duration_ms": None,
                "last_fetched": 0,
                "last_written": 0,
                "last_snapshots": 0,
                "consecutive_failures": 0,
                "success_count": 0,
                "failure_count": 0,
            }
            for name in _JOB_LABELS
        }
        self._provider_status: Dict[str, Dict[str, Any]] = {
            "tradingeconomics": {
                "enabled": self.settings.tradingeconomics_enabled,
                "last_success_at": None,
                "last_error": None,
                "consecutive_failures": 0,
                "last_event_count": 0,
            },
            "fred": {
                "enabled": self.settings.fred_enabled,
                "last_success_at": None,
                "last_error": None,
                "consecutive_failures": 0,
                "last_event_count": 0,
            },
        }

    def attach_storage_writer(self, storage_writer: Optional[StorageWriter]) -> None:
        self.storage_writer = storage_writer

    @staticmethod
    def _coerce_date(value: Optional[date | datetime]) -> date:
        if value is None:
            return _utc_now().date()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc).date()
        return value

    @staticmethod
    def _coerce_datetime(value: Optional[date | datetime]) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        return _start_of_day(value)

    def _job_interval(self, job_type: str) -> float:
        if job_type == "calendar_sync":
            return float(self.settings.calendar_sync_interval_seconds)
        if job_type == "near_term_sync":
            return float(self.settings.near_term_refresh_interval_seconds or self.settings.refresh_interval_seconds)
        if job_type == "release_watch":
            return float(self.settings.release_watch_interval_seconds)
        return 0.0

    def default_window(self) -> tuple[date, date]:
        today = _utc_now().date()
        return (
            today - timedelta(days=self.settings.lookback_days),
            today + timedelta(days=self.settings.lookahead_days),
        )

    def _default_sources_for_job(self, job_type: str) -> List[str]:
        if job_type == "release_watch":
            return ["tradingeconomics"]
        return ["tradingeconomics", "fred"]

    def _normalize_sources(self, sources: Optional[List[str]], job_type: str) -> List[str]:
        requested = [source.strip().lower() for source in (sources or self._default_sources_for_job(job_type)) if source]
        return [source for source in requested if source in {"tradingeconomics", "fred"}]

    def _job_window(self, job_type: str) -> tuple[datetime, datetime]:
        now = _utc_now()
        if job_type == "calendar_sync":
            start_date, end_date = self.default_window()
            return _start_of_day(start_date), _end_of_day(end_date)
        if job_type == "near_term_sync":
            return now, now + timedelta(hours=max(1, self.settings.near_term_window_hours))
        if job_type == "release_watch":
            return (
                now - timedelta(minutes=max(0, self.settings.release_watch_lookback_minutes)),
                now + timedelta(minutes=max(1, self.settings.release_watch_lookahead_minutes)),
            )
        raise EconomicCalendarError(f"Unsupported economic job type: {job_type}")

    def _event_within_bounds(self, event: EconomicCalendarEvent, start_at: datetime, end_at: datetime) -> bool:
        if event.all_day:
            event_day = event.scheduled_at.astimezone(timezone.utc).date()
            return start_at.date() <= event_day <= end_at.date()
        scheduled_at = event.scheduled_at.astimezone(timezone.utc)
        return start_at <= scheduled_at <= end_at

    def _enrich_events(self, events: List[EconomicCalendarEvent]) -> List[EconomicCalendarEvent]:
        return [event.enrich_time_context(self.settings.local_timezone) for event in events]

    def _update_provider_success(self, provider: str, event_count: int) -> None:
        state = self._provider_status[provider]
        state["last_success_at"] = _utc_now().isoformat()
        state["last_error"] = None
        state["consecutive_failures"] = 0
        state["last_event_count"] = event_count

    def _update_provider_failure(self, provider: str, exc: Exception) -> None:
        state = self._provider_status[provider]
        state["last_error"] = str(exc)
        state["consecutive_failures"] = int(state["consecutive_failures"]) + 1
        state["last_event_count"] = 0

    def _fetch_from_provider(
        self,
        provider: str,
        start_date: date,
        end_date: date,
        countries: Optional[List[str]],
    ) -> List[EconomicCalendarEvent]:
        try:
            if provider == "tradingeconomics":
                events = self._te_client.fetch_events(start_date, end_date, countries=countries)
            elif provider == "fred":
                events = self._fred_client.fetch_events(start_date, end_date)
            else:
                return []
            self._update_provider_success(provider, len(events))
            return events
        except Exception as exc:
            self._update_provider_failure(provider, exc)
            raise

    def _derive_status(self, event: EconomicCalendarEvent, observed_at: datetime) -> str:
        if event.actual or event.revised:
            return "released"
        if event.all_day and observed_at.date() > event.scheduled_at.date():
            return "released"
        imminent_at = event.scheduled_at - timedelta(minutes=max(0, self.settings.pre_event_buffer_minutes))
        if observed_at >= event.scheduled_at:
            return "pending_release"
        if observed_at >= imminent_at:
            return "imminent"
        return "scheduled"

    def _apply_lifecycle(
        self,
        event: EconomicCalendarEvent,
        existing: Optional[EconomicCalendarEvent],
        observed_at: datetime,
        *,
        value_check: bool,
    ) -> EconomicCalendarEvent:
        event.status = self._derive_status(event, observed_at)
        event.first_seen_at = existing.first_seen_at if existing else observed_at
        event.last_seen_at = observed_at
        event.ingested_at = existing.ingested_at if existing else observed_at
        event.last_updated = observed_at
        event.released_at = existing.released_at if existing else None
        if event.status == "released" and event.released_at is None:
            event.released_at = observed_at
        event.last_value_check_at = (
            observed_at
            if value_check or event.status in {"pending_release", "released"}
            else (existing.last_value_check_at if existing else None)
        )
        return event

    @staticmethod
    def _snapshot_reason(
        existing: Optional[EconomicCalendarEvent],
        event: EconomicCalendarEvent,
    ) -> Optional[str]:
        if existing is None:
            return "inserted"
        if (
            existing.actual != event.actual
            or existing.previous != event.previous
            or existing.forecast != event.forecast
            or existing.revised != event.revised
            or existing.released_at != event.released_at
        ):
            return "values_changed"
        if existing.scheduled_at != event.scheduled_at:
            return "schedule_changed"
        if existing.status != event.status:
            return "status_changed"
        if (
            existing.event_name != event.event_name
            or existing.country != event.country
            or existing.category != event.category
            or existing.currency != event.currency
            or existing.reference != event.reference
            or existing.importance != event.importance
            or existing.unit != event.unit
            or existing.source_url != event.source_url
            or existing.session_bucket != event.session_bucket
        ):
            return "metadata_changed"
        return None

    def _fetch_existing_by_uid(self, events):
        return fetch_existing_by_uid(self, events)

    def _write_events(self, job_type: str, events, *, value_check: bool):
        return write_events(self, job_type, events, value_check=value_check)

    def _store_with_backpressure_control(self, channel: str, rows):
        return store_with_backpressure_control(self, channel, rows)

    def _fetch_job_events(self, **kwargs):
        return fetch_job_events(self, **kwargs)

    def _job_summary(self, **kwargs):
        return job_summary(**kwargs)

    def _schedule_next_run(self, job_type: str, *, from_time: Optional[datetime] = None) -> None:
        schedule_next_run(self, job_type, from_time=from_time)

    def _update_job_state(self, job_type: str, **kwargs) -> None:
        update_job_state(self, job_type, **kwargs)

    def _persist_job_state(self, job_type: str) -> None:
        persist_job_state(self, job_type)

    def _restore_job_state(self) -> None:
        restore_job_state(self)

    def _startup_schedule_time(self, job_type: str, now: datetime) -> Optional[datetime]:
        return startup_schedule_time(self, job_type, now)

    def _run_job(self, **kwargs):
        return run_job(self, **kwargs)

    def _safe_run_job(self, job_type: str) -> None:
        safe_run_job(self, job_type)

    def _run_scheduler(self) -> None:
        run_scheduler(self)

    def _ensure_worker_running(self) -> None:
        ensure_worker_running(self)

    def start(self) -> None:
        start_service(self)

    def stop(self) -> None:
        stop_service(self)

    def refresh(
        self,
        start_date: Optional[date | datetime] = None,
        end_date: Optional[date | datetime] = None,
        countries: Optional[List[str]] = None,
        sources: Optional[List[str]] = None,
        job_type: str = "calendar_sync",
    ) -> Dict[str, Any]:
        return refresh_service(
            self,
            start_date=start_date,
            end_date=end_date,
            countries=countries,
            sources=sources,
            job_type=job_type,
        )

    def get_events(
        self,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: int = 1000,
        sources: Optional[List[str]] = None,
        countries: Optional[List[str]] = None,
        currencies: Optional[List[str]] = None,
        session_buckets: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        importance_min: Optional[int] = None,
    ) -> List[EconomicCalendarEvent]:
        self._ensure_worker_running()
        rows = self.db.fetch_economic_calendar(
            start_time,
            end_time,
            limit,
            sources=sources,
            countries=countries,
            currencies=currencies,
            session_buckets=session_buckets,
            statuses=statuses,
            importance_min=importance_min,
        )
        return [EconomicCalendarEvent.from_db_row(row) for row in rows]

    def get_upcoming(
        self,
        hours: int = 24,
        limit: int = 200,
        sources: Optional[List[str]] = None,
        countries: Optional[List[str]] = None,
        currencies: Optional[List[str]] = None,
        session_buckets: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        importance_min: Optional[int] = None,
    ) -> List[EconomicCalendarEvent]:
        self._ensure_worker_running()
        start_time = _utc_now()
        end_time = start_time + timedelta(hours=hours)
        return self.get_events(
            start_time,
            end_time,
            limit=limit,
            sources=sources,
            countries=countries,
            currencies=currencies,
            session_buckets=session_buckets,
            statuses=statuses,
            importance_min=importance_min,
        )

    def get_high_impact_events(
        self,
        hours: int = 24,
        limit: int = 200,
        sources: Optional[List[str]] = None,
        countries: Optional[List[str]] = None,
        currencies: Optional[List[str]] = None,
        session_buckets: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        importance_min: Optional[int] = None,
    ) -> List[EconomicCalendarEvent]:
        threshold = importance_min if importance_min is not None else self.settings.high_importance_threshold
        return self.get_upcoming(
            hours=hours,
            limit=limit,
            sources=sources,
            countries=countries,
            currencies=currencies,
            session_buckets=session_buckets,
            statuses=statuses,
            importance_min=threshold,
        )

    def get_curated_events(
        self,
        hours: int = 24,
        limit: int = 200,
        sources: Optional[List[str]] = None,
        countries: Optional[List[str]] = None,
        currencies: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        importance_min: Optional[int] = None,
        include_all_day: Optional[bool] = None,
    ) -> List[EconomicCalendarEvent]:
        effective_sources = sources if sources is not None else (self.settings.curated_sources or None)
        effective_countries = countries if countries is not None else (self.settings.curated_countries or None)
        effective_currencies = currencies if currencies is not None else (self.settings.curated_currencies or None)
        effective_statuses = statuses if statuses is not None else (self.settings.curated_statuses or None)
        effective_importance = importance_min if importance_min is not None else self.settings.curated_importance_min
        allow_all_day = self.settings.curated_include_all_day if include_all_day is None else include_all_day

        items = self.get_upcoming(
            hours=hours,
            limit=limit,
            sources=effective_sources,
            countries=effective_countries,
            currencies=effective_currencies,
            statuses=effective_statuses,
            importance_min=effective_importance,
        )
        if allow_all_day:
            return items
        return [item for item in items if not item.all_day]

    def _compute_window_bounds(self, event: EconomicCalendarEvent):
        return compute_window_bounds(self, event)

    def _build_window(self, event: EconomicCalendarEvent):
        return build_window(self, event)

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        return normalize_symbol(symbol)

    def infer_symbol_context(self, symbol: str) -> Dict[str, List[str]]:
        return infer_symbol_context(symbol)

    @staticmethod
    def _window_to_datetimes(window: Dict[str, Any]) -> Dict[str, Any]:
        return window_to_datetimes(window)

    def _merge_windows(self, windows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return merge_windows(windows)

    def get_risk_windows(self, **kwargs) -> List[Dict[str, Any]]:
        return get_risk_windows(self, **kwargs)

    def get_merged_risk_windows(self, **kwargs) -> List[Dict[str, Any]]:
        return get_merged_risk_windows(self, **kwargs)

    def get_trade_guard(
        self,
        symbol: str,
        at_time: Optional[datetime] = None,
        lookahead_minutes: int = 180,
        lookback_minutes: int = 0,
        limit: int = 500,
        importance_min: Optional[int] = None,
    ) -> Dict[str, Any]:
        return get_trade_guard(
            self,
            symbol=symbol,
            at_time=at_time,
            lookahead_minutes=lookahead_minutes,
            lookback_minutes=lookback_minutes,
            limit=limit,
            importance_min=importance_min,
        )

    def get_updates(
        self,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: int = 500,
        event_uid: Optional[str] = None,
        snapshot_reasons: Optional[List[str]] = None,
        job_types: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        return get_update_rows(
            self,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            event_uid=event_uid,
            snapshot_reasons=snapshot_reasons,
            job_types=job_types,
        )

    def is_stale(self) -> bool:
        if self._last_refresh_at is None:
            return True
        return (_utc_now() - self._last_refresh_at).total_seconds() > float(self.settings.stale_after_seconds)

    def stats(self) -> Dict[str, Any]:
        return build_stats(self)
