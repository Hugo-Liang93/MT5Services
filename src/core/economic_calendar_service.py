from __future__ import annotations

import logging
import random
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from threading import Event, Lock, RLock, Thread
from typing import Any, Dict, List, Optional, Sequence

from src.clients.economic_calendar import (
    EconomicCalendarError,
    EconomicCalendarEvent,
    FredCalendarClient,
    TradingEconomicsCalendarClient,
)
from src.config import EconomicConfig, get_economic_config
from src.persistence.db import TimescaleWriter
from src.persistence.storage_writer import StorageWriter

logger = logging.getLogger(__name__)

_CURRENCY_TO_COUNTRY = {
    "AUD": "Australia",
    "CAD": "Canada",
    "CHF": "Switzerland",
    "CNY": "China",
    "EUR": "Euro Area",
    "GBP": "United Kingdom",
    "JPY": "Japan",
    "NZD": "New Zealand",
    "USD": "United States",
}

_INDEX_SYMBOL_COUNTRY_HINTS = {
    "US": "United States",
    "DE": "Germany",
    "GER": "Germany",
    "UK": "United Kingdom",
    "JP": "Japan",
    "HK": "China",
    "CN": "China",
    "AU": "Australia",
}

_JOB_LABELS = {
    "calendar_sync": "calendar_sync",
    "near_term_sync": "near_term_sync",
    "release_watch": "release_watch",
}
_RUNTIME_COMPONENT = "economic_calendar"
_DIRECT_WRITE_CHANNELS = {
    "economic_calendar": "write_economic_calendar",
    "economic_calendar_updates": "write_economic_calendar_updates",
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

    def _fetch_existing_by_uid(self, events: Sequence[EconomicCalendarEvent]) -> Dict[str, EconomicCalendarEvent]:
        event_uids = sorted({event.event_uid for event in events})
        rows = self.db.fetch_economic_calendar_by_uids(event_uids)
        return {event.event_uid: event for event in (EconomicCalendarEvent.from_db_row(row) for row in rows)}

    def _write_events(
        self,
        job_type: str,
        events: List[EconomicCalendarEvent],
        *,
        value_check: bool,
    ) -> Dict[str, int]:
        if not events:
            return {"written": 0, "snapshots_written": 0, "deleted": 0}
        observed_at = _utc_now()
        existing_map = self._fetch_existing_by_uid(events)
        rows: List[tuple] = []
        update_rows: List[tuple] = []
        delete_keys: List[tuple[datetime, str]] = []

        for event in events:
            existing = existing_map.get(event.event_uid)
            prepared = self._apply_lifecycle(event, existing, observed_at, value_check=value_check)
            if existing and existing.scheduled_at != prepared.scheduled_at:
                delete_keys.append((existing.scheduled_at, existing.event_uid))
            reason = self._snapshot_reason(existing, prepared)
            rows.append(prepared.to_row())
            if reason is not None:
                update_rows.append(
                    prepared.to_update_row(
                        recorded_at=observed_at,
                        snapshot_reason=reason,
                        job_type=job_type,
                    )
                    )

        with self._lock:
            if delete_keys:
                self.db.delete_economic_calendar_by_keys(delete_keys)
            if self.storage_writer is None:
                self.db.write_economic_calendar(rows)
                if update_rows:
                    self.db.write_economic_calendar_updates(update_rows)
            else:
                self._store_with_backpressure_control("economic_calendar", rows)
                self._store_with_backpressure_control("economic_calendar_updates", update_rows)

        return {
            "written": len(rows),
            "snapshots_written": len(update_rows),
            "deleted": len(delete_keys),
        }

    def _store_with_backpressure_control(self, channel: str, rows: List[tuple]) -> None:
        if not rows:
            return
        if self.storage_writer is None:
            getattr(self.db, _DIRECT_WRITE_CHANNELS[channel])(rows)
            return
        batch_size = self.storage_writer.get_channel_batch_size(channel) or len(rows)
        if len(rows) >= max(50, batch_size):
            self.storage_writer.write_now(channel, rows)
            return
        for row in rows:
            self.storage_writer.enqueue(channel, row)

    def _fetch_job_events(
        self,
        *,
        job_type: str,
        start_at: datetime,
        end_at: datetime,
        countries: Optional[List[str]],
        sources: Optional[List[str]],
    ) -> tuple[List[EconomicCalendarEvent], Dict[str, int], Dict[str, str]]:
        provider_counts: Dict[str, int] = {}
        provider_errors: Dict[str, str] = {}
        events: List[EconomicCalendarEvent] = []
        for provider in self._normalize_sources(sources, job_type):
            if provider == "tradingeconomics" and not self.settings.tradingeconomics_enabled:
                continue
            if provider == "fred" and not self.settings.fred_enabled:
                continue
            try:
                fetched = self._fetch_from_provider(provider, start_at.date(), end_at.date(), countries)
                provider_counts[provider] = len(fetched)
                events.extend(fetched)
            except Exception as exc:
                provider_errors[provider] = str(exc)
        if provider_errors and not provider_counts:
            raise EconomicCalendarError(f"All economic calendar providers failed: {provider_errors}")

        enriched_events = self._enrich_events(events)
        filtered = [event for event in enriched_events if self._event_within_bounds(event, start_at, end_at)]
        deduped = {event.event_uid: event for event in sorted(filtered, key=lambda item: item.scheduled_at)}
        return list(deduped.values()), provider_counts, provider_errors

    def _job_summary(
        self,
        *,
        job_type: str,
        status: str,
        fetched: int,
        written: int,
        snapshots_written: int,
        start_at: datetime,
        end_at: datetime,
        duration_ms: int,
        provider_counts: Dict[str, int],
        provider_errors: Dict[str, str],
        deleted: int = 0,
    ) -> Dict[str, Any]:
        return {
            "job_type": job_type,
            "status": status,
            "fetched": fetched,
            "written": written,
            "snapshots_written": snapshots_written,
            "deleted": deleted,
            "start_date": start_at.date().isoformat(),
            "end_date": end_at.date().isoformat(),
            "start_time": start_at.isoformat(),
            "end_time": end_at.isoformat(),
            "duration_ms": str(duration_ms),
            "provider_counts": str(provider_counts),
            "provider_errors": str(provider_errors) if provider_errors else "",
        }

    def _schedule_next_run(self, job_type: str, *, from_time: Optional[datetime] = None) -> None:
        interval = self._job_interval(job_type)
        self._next_run_at[job_type] = None if interval <= 0 else (from_time or _utc_now()) + timedelta(seconds=interval)

    def _update_job_state(
        self,
        job_type: str,
        *,
        started_at: datetime,
        completed_at: datetime,
        status: str,
        fetched: int,
        written: int,
        snapshots_written: int,
        duration_ms: int,
        error: Optional[str],
    ) -> None:
        job_state = self._job_state[job_type]
        job_state["last_started_at"] = started_at.isoformat()
        job_state["last_completed_at"] = completed_at.isoformat()
        job_state["last_error"] = error
        job_state["last_status"] = status
        job_state["last_duration_ms"] = duration_ms
        job_state["last_fetched"] = fetched
        job_state["last_written"] = written
        job_state["last_snapshots"] = snapshots_written
        if error:
            job_state["failure_count"] = int(job_state["failure_count"]) + 1
            job_state["consecutive_failures"] = int(job_state["consecutive_failures"]) + 1
        else:
            job_state["success_count"] = int(job_state["success_count"]) + 1
            job_state["consecutive_failures"] = 0
        self._schedule_next_run(job_type, from_time=completed_at)
        self._persist_job_state(job_type)

    def _runtime_task_details(self, job_type: str) -> Dict[str, Any]:
        job_state = self._job_state[job_type]
        return {
            "enabled": bool(job_state["enabled"]),
            "interval_seconds": self._job_interval(job_type),
            "last_fetched": int(job_state["last_fetched"]),
            "last_written": int(job_state["last_written"]),
            "last_snapshots": int(job_state["last_snapshots"]),
            "provider_status": self._provider_status,
        }

    def _runtime_task_row(self, job_type: str) -> tuple:
        job_state = self._job_state[job_type]
        updated_at = _utc_now()
        started_at = (
            datetime.fromisoformat(job_state["last_started_at"])
            if job_state.get("last_started_at")
            else None
        )
        completed_at = (
            datetime.fromisoformat(job_state["last_completed_at"])
            if job_state.get("last_completed_at")
            else None
        )
        next_run_at = self._next_run_at.get(job_type)
        return (
            _RUNTIME_COMPONENT,
            job_type,
            updated_at,
            str(job_state.get("last_status") or ("idle" if job_state["enabled"] else "disabled")),
            started_at,
            completed_at,
            next_run_at,
            job_state.get("last_duration_ms"),
            int(job_state.get("success_count", 0)),
            int(job_state.get("failure_count", 0)),
            int(job_state.get("consecutive_failures", 0)),
            job_state.get("last_error"),
            self._runtime_task_details(job_type),
        )

    def _persist_job_state(self, job_type: str) -> None:
        try:
            self.db.write_runtime_task_status([self._runtime_task_row(job_type)])
        except Exception as exc:  # pragma: no cover
            logger.debug("Failed to persist economic runtime task status for %s: %s", job_type, exc)

    def _restore_job_state(self) -> None:
        try:
            rows = self.db.fetch_runtime_task_status(component=_RUNTIME_COMPONENT)
        except Exception as exc:  # pragma: no cover
            logger.debug("Failed to restore economic runtime task state: %s", exc)
            return
        for row in rows:
            _, task_name, _, state, started_at, completed_at, next_run_at, duration_ms, success_count, failure_count, consecutive_failures, last_error, details = row
            if task_name not in self._job_state:
                continue
            job_state = self._job_state[task_name]
            job_state["last_status"] = state
            job_state["last_started_at"] = started_at.isoformat() if started_at else None
            job_state["last_completed_at"] = completed_at.isoformat() if completed_at else None
            job_state["last_duration_ms"] = duration_ms
            job_state["success_count"] = int(success_count or 0)
            job_state["failure_count"] = int(failure_count or 0)
            job_state["consecutive_failures"] = int(consecutive_failures or 0)
            job_state["last_error"] = last_error
            if isinstance(details, dict):
                job_state["last_fetched"] = int(details.get("last_fetched", job_state["last_fetched"]))
                job_state["last_written"] = int(details.get("last_written", job_state["last_written"]))
                job_state["last_snapshots"] = int(details.get("last_snapshots", job_state["last_snapshots"]))
            if next_run_at is not None:
                self._next_run_at[task_name] = next_run_at

    def _startup_schedule_time(self, job_type: str, now: datetime) -> Optional[datetime]:
        interval = self._job_interval(job_type)
        if interval <= 0:
            return None
        jitter = min(max(0.0, float(self.settings.refresh_jitter_seconds)), max(0.0, interval))
        delay_seconds = interval
        if job_type == "calendar_sync" and self.settings.startup_refresh:
            delay_seconds = max(0.0, float(self.settings.startup_calendar_sync_delay_seconds))
        scheduled_at = now + timedelta(seconds=delay_seconds)
        if jitter > 0:
            scheduled_at += timedelta(seconds=random.uniform(0.0, jitter))
        return scheduled_at

    def _run_job(
        self,
        *,
        job_type: str,
        start_at: datetime,
        end_at: datetime,
        countries: Optional[List[str]],
        sources: Optional[List[str]],
    ) -> Dict[str, Any]:
        started_at = _utc_now()
        fetch_started = time.monotonic()
        try:
            events, provider_counts, provider_errors = self._fetch_job_events(
                job_type=job_type,
                start_at=start_at,
                end_at=end_at,
                countries=countries or self.settings.default_countries or None,
                sources=sources,
            )
            write_result = self._write_events(
                job_type,
                sorted(events, key=lambda item: item.scheduled_at),
                value_check=job_type in {"near_term_sync", "release_watch"},
            )
            completed_at = _utc_now()
            duration_ms = int((time.monotonic() - fetch_started) * 1000)
            summary = self._job_summary(
                job_type=job_type,
                status="ok" if not provider_errors else "partial",
                fetched=len(events),
                written=write_result["written"],
                snapshots_written=write_result["snapshots_written"],
                deleted=write_result["deleted"],
                start_at=start_at,
                end_at=end_at,
                duration_ms=duration_ms,
                provider_counts=provider_counts,
                provider_errors=provider_errors,
            )
            self._last_refresh_at = completed_at
            self._last_refresh_started_at = started_at
            self._last_refresh_completed_at = completed_at
            self._last_refresh_duration_ms = duration_ms
            self._last_refresh_error = None if not provider_errors else str(provider_errors)
            self._last_refresh_summary = summary
            self._consecutive_failures = 0
            self._update_job_state(
                job_type,
                started_at=started_at,
                completed_at=completed_at,
                status=summary["status"],
                fetched=summary["fetched"],
                written=summary["written"],
                snapshots_written=summary["snapshots_written"],
                duration_ms=duration_ms,
                error=None,
            )
            return summary
        except Exception as exc:
            completed_at = _utc_now()
            duration_ms = int((time.monotonic() - fetch_started) * 1000)
            self._last_refresh_started_at = started_at
            self._last_refresh_completed_at = completed_at
            self._last_refresh_duration_ms = duration_ms
            self._last_refresh_error = str(exc)
            self._consecutive_failures += 1
            self._update_job_state(
                job_type,
                started_at=started_at,
                completed_at=completed_at,
                status="error",
                fetched=0,
                written=0,
                snapshots_written=0,
                duration_ms=duration_ms,
                error=str(exc),
            )
            raise

    def _safe_run_job(self, job_type: str) -> None:
        try:
            self.refresh(job_type=job_type)
        except EconomicCalendarError as exc:
            self._last_refresh_error = str(exc)
            logger.warning("Economic calendar %s failed: %s", job_type, exc)
        except Exception as exc:  # pragma: no cover
            self._last_refresh_error = str(exc)
            logger.exception("Unexpected economic calendar %s failure: %s", job_type, exc)

    def _run_scheduler(self) -> None:
        while True:
            now = _utc_now()
            due_jobs = [
                job_type
                for job_type, next_run in self._next_run_at.items()
                if next_run is not None and next_run <= now and self._job_interval(job_type) > 0
            ]
            if due_jobs:
                for job_type in sorted(due_jobs, key=self._job_interval):
                    self._safe_run_job(job_type)
                continue
            upcoming = [next_run for next_run in self._next_run_at.values() if next_run is not None]
            wait_seconds = 5.0
            if upcoming:
                wait_seconds = max(1.0, min(5.0, min((next_run - now).total_seconds() for next_run in upcoming)))
            if self._stop_event.wait(wait_seconds):
                break

    def _ensure_worker_running(self) -> None:
        if not self.settings.enabled:
            return
        if not any(self._job_interval(job_type) > 0 for job_type in _JOB_LABELS):
            return
        if self._worker and self._worker.is_alive():
            return
        self.start()

    def start(self) -> None:
        if not self.settings.enabled:
            logger.info("Economic calendar service is disabled")
            return
        self._restore_job_state()
        if self.settings.startup_refresh:
            for job_type in ("near_term_sync", "release_watch"):
                self._safe_run_job(job_type)
        if not any(self._job_interval(job_type) > 0 for job_type in _JOB_LABELS):
            logger.info("Economic calendar background refresh disabled by interval <= 0")
            return
        if self._worker and self._worker.is_alive():
            return
        self._stop_event.clear()
        now = _utc_now()
        for job_type in _JOB_LABELS:
            interval = self._job_interval(job_type)
            scheduled_at = self._startup_schedule_time(job_type, now)
            restored_next_run = self._next_run_at.get(job_type)
            if interval <= 0:
                self._next_run_at[job_type] = None
            elif restored_next_run is None or restored_next_run <= now:
                self._next_run_at[job_type] = scheduled_at
            elif job_type == "calendar_sync" and self.settings.startup_refresh and scheduled_at is not None:
                self._next_run_at[job_type] = max(restored_next_run, scheduled_at)
            self._persist_job_state(job_type)
        self._worker = Thread(target=self._run_scheduler, name="economic-calendar-refresh", daemon=True)
        self._worker.start()
        logger.info(
            "Economic calendar scheduler started: calendar=%ss startup_delay=%ss near_term=%ss release_watch=%ss local_timezone=%s",
            self.settings.calendar_sync_interval_seconds,
            self.settings.startup_calendar_sync_delay_seconds,
            self.settings.near_term_refresh_interval_seconds,
            self.settings.release_watch_interval_seconds,
            self.settings.local_timezone,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=5.0)
        self._worker = None
        for job_type in _JOB_LABELS:
            self._job_state[job_type]["last_status"] = "stopped"
            self._persist_job_state(job_type)

    def refresh(
        self,
        start_date: Optional[date | datetime] = None,
        end_date: Optional[date | datetime] = None,
        countries: Optional[List[str]] = None,
        sources: Optional[List[str]] = None,
        job_type: str = "calendar_sync",
    ) -> Dict[str, Any]:
        job_type = _JOB_LABELS.get(job_type, job_type)
        if job_type not in _JOB_LABELS:
            raise EconomicCalendarError(f"Unsupported economic job type: {job_type}")
        if not self.settings.enabled:
            return {"status": "disabled", "job_type": job_type, "written": 0, "fetched": 0, "snapshots_written": 0}
        if not self._refresh_lock.acquire(blocking=False):
            return {"status": "refresh_in_progress", "job_type": job_type, "written": 0, "fetched": 0, "snapshots_written": 0}
        self._refresh_in_progress = True
        try:
            default_start_at, default_end_at = self._job_window(job_type)
            start_at = self._coerce_datetime(start_date) if start_date is not None else default_start_at
            end_at = self._coerce_datetime(end_date) if end_date is not None else default_end_at
            if start_at is None or end_at is None:
                raise EconomicCalendarError("Economic calendar refresh window is invalid")
            if isinstance(start_date, date) and not isinstance(start_date, datetime):
                start_at = _start_of_day(start_date)
            if isinstance(end_date, date) and not isinstance(end_date, datetime):
                end_at = _end_of_day(end_date)
            return self._run_job(job_type=job_type, start_at=start_at, end_at=end_at, countries=countries, sources=sources)
        finally:
            self._refresh_in_progress = False
            self._refresh_lock.release()

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

    def _compute_window_bounds(self, event: EconomicCalendarEvent) -> tuple[datetime, datetime]:
        start = event.scheduled_at - timedelta(minutes=self.settings.pre_event_buffer_minutes)
        end = event.scheduled_at + timedelta(minutes=self.settings.post_event_buffer_minutes)
        return start, end

    def _build_window(self, event: EconomicCalendarEvent) -> Dict[str, Any]:
        window_start, window_end = self._compute_window_bounds(event)
        return {
            "event_uid": event.event_uid,
            "event_name": event.event_name,
            "source": event.source,
            "country": event.country,
            "currency": event.currency,
            "importance": event.importance,
            "session_bucket": event.session_bucket,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "scheduled_at": event.scheduled_at.isoformat(),
            "scheduled_at_local": event.scheduled_at_local.isoformat() if event.scheduled_at_local else None,
            "scheduled_at_release": event.scheduled_at_release.isoformat() if event.scheduled_at_release else None,
        }

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        return "".join(ch for ch in (symbol or "").upper() if ch.isalnum())

    def infer_symbol_context(self, symbol: str) -> Dict[str, List[str]]:
        normalized = self._normalize_symbol(symbol)
        currencies: set[str] = set()
        countries: set[str] = set()
        if len(normalized) >= 6 and normalized[:6].isalpha():
            maybe_base = normalized[:3]
            maybe_quote = normalized[3:6]
            if maybe_base in _CURRENCY_TO_COUNTRY and maybe_quote in _CURRENCY_TO_COUNTRY:
                currencies.update([maybe_base, maybe_quote])
        for currency, country in _CURRENCY_TO_COUNTRY.items():
            if currency in normalized:
                currencies.add(currency)
                countries.add(country)
        for prefix, country in _INDEX_SYMBOL_COUNTRY_HINTS.items():
            if normalized.startswith(prefix):
                countries.add(country)
        if not countries:
            for currency in currencies:
                countries.add(_CURRENCY_TO_COUNTRY[currency])
        return {
            "symbol": symbol,
            "normalized_symbol": normalized,
            "currencies": sorted(currencies),
            "countries": sorted(countries),
        }

    @staticmethod
    def _window_to_datetimes(window: Dict[str, Any]) -> Dict[str, Any]:
        return {
            **window,
            "window_start_dt": datetime.fromisoformat(window["window_start"]),
            "window_end_dt": datetime.fromisoformat(window["window_end"]),
        }

    def _merge_windows(self, windows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not windows:
            return []
        normalized = sorted((self._window_to_datetimes(window) for window in windows), key=lambda item: item["window_start_dt"])
        merged: List[Dict[str, Any]] = []
        for window in normalized:
            if not merged or window["window_start_dt"] > merged[-1]["window_end_dt"]:
                merged.append(
                    {
                        "window_start_dt": window["window_start_dt"],
                        "window_end_dt": window["window_end_dt"],
                        "event_uids": [window["event_uid"]],
                        "event_names": [window["event_name"]],
                        "sources": sorted({window["source"]} if window["source"] else set()),
                        "countries": sorted({window["country"]} if window["country"] else set()),
                        "currencies": sorted({window["currency"]} if window["currency"] else set()),
                        "sessions": sorted({window["session_bucket"]} if window["session_bucket"] else set()),
                        "max_importance": window["importance"],
                    }
                )
                continue
            current = merged[-1]
            if window["window_end_dt"] > current["window_end_dt"]:
                current["window_end_dt"] = window["window_end_dt"]
            current["event_uids"].append(window["event_uid"])
            current["event_names"].append(window["event_name"])
            if window["source"]:
                current["sources"] = sorted(set(current["sources"]) | {window["source"]})
            if window["country"]:
                current["countries"] = sorted(set(current["countries"]) | {window["country"]})
            if window["currency"]:
                current["currencies"] = sorted(set(current["currencies"]) | {window["currency"]})
            if window["session_bucket"]:
                current["sessions"] = sorted(set(current["sessions"]) | {window["session_bucket"]})
            if window["importance"] is not None:
                current["max_importance"] = window["importance"] if current["max_importance"] is None else max(current["max_importance"], window["importance"])
        return [
            {
                "window_start": item["window_start_dt"].isoformat(),
                "window_end": item["window_end_dt"].isoformat(),
                "event_count": len(item["event_uids"]),
                "event_uids": item["event_uids"],
                "event_names": item["event_names"],
                "sources": item["sources"],
                "countries": item["countries"],
                "currencies": item["currencies"],
                "sessions": item["sessions"],
                "max_importance": item["max_importance"],
            }
            for item in merged
        ]

    def get_risk_windows(
        self,
        hours: int = 24,
        limit: int = 200,
        sources: Optional[List[str]] = None,
        countries: Optional[List[str]] = None,
        currencies: Optional[List[str]] = None,
        session_buckets: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        importance_min: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        events = self.get_high_impact_events(
            hours=hours,
            limit=limit,
            sources=sources,
            countries=countries,
            currencies=currencies,
            session_buckets=session_buckets,
            statuses=statuses,
            importance_min=importance_min,
        )
        return [self._build_window(event) for event in events]

    def get_merged_risk_windows(
        self,
        hours: int = 24,
        limit: int = 200,
        sources: Optional[List[str]] = None,
        countries: Optional[List[str]] = None,
        currencies: Optional[List[str]] = None,
        session_buckets: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        importance_min: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        windows = self.get_risk_windows(
            hours=hours,
            limit=limit,
            sources=sources,
            countries=countries,
            currencies=currencies,
            session_buckets=session_buckets,
            statuses=statuses,
            importance_min=importance_min,
        )
        return self._merge_windows(windows)

    def get_trade_guard(
        self,
        symbol: str,
        at_time: Optional[datetime] = None,
        lookahead_minutes: int = 180,
        lookback_minutes: int = 0,
        limit: int = 500,
        importance_min: Optional[int] = None,
    ) -> Dict[str, Any]:
        self._ensure_worker_running()
        evaluation_time = at_time or _utc_now()
        if evaluation_time.tzinfo is None:
            evaluation_time = evaluation_time.replace(tzinfo=timezone.utc)
        else:
            evaluation_time = evaluation_time.astimezone(timezone.utc)
        context = self.infer_symbol_context(symbol)
        events = self.get_events(
            start_time=evaluation_time - timedelta(minutes=max(0, lookback_minutes)),
            end_time=evaluation_time + timedelta(minutes=max(0, lookahead_minutes)),
            limit=limit,
            countries=context["countries"] or None,
            currencies=context["currencies"] or None,
            statuses=["scheduled", "imminent", "pending_release", "released"],
            importance_min=importance_min if importance_min is not None else self.settings.high_importance_threshold,
        )
        merged_windows = self._merge_windows([self._build_window(event) for event in events])
        active_windows = [
            window
            for window in merged_windows
            if datetime.fromisoformat(window["window_start"]) <= evaluation_time <= datetime.fromisoformat(window["window_end"])
        ]
        upcoming_windows = [
            window
            for window in merged_windows
            if evaluation_time < datetime.fromisoformat(window["window_start"])
        ]
        return {
            "symbol": symbol,
            "evaluation_time": evaluation_time.isoformat(),
            "blocked": bool(active_windows),
            "currencies": context["currencies"],
            "countries": context["countries"],
            "active_windows": active_windows,
            "upcoming_windows": upcoming_windows,
            "importance_min": importance_min if importance_min is not None else self.settings.high_importance_threshold,
        }

    def get_updates(
        self,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: int = 500,
        event_uid: Optional[str] = None,
        snapshot_reasons: Optional[List[str]] = None,
        job_types: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        rows = self.db.fetch_economic_calendar_updates(
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            event_uid=event_uid,
            snapshot_reasons=snapshot_reasons,
            job_types=job_types,
        )
        return [
            {
                "recorded_at": row[0].isoformat(),
                "event_uid": row[1],
                "scheduled_at": row[2].isoformat(),
                "source": row[3],
                "provider_event_id": row[4],
                "event_name": row[5],
                "country": row[6],
                "currency": row[7],
                "status": row[8],
                "snapshot_reason": row[9],
                "job_type": row[10],
                "actual": row[11],
                "previous": row[12],
                "forecast": row[13],
                "revised": row[14],
                "importance": row[15],
                "raw_payload": row[16] or {},
            }
            for row in rows
        ]

    def is_stale(self) -> bool:
        if self._last_refresh_at is None:
            return True
        return (_utc_now() - self._last_refresh_at).total_seconds() > float(self.settings.stale_after_seconds)

    def stats(self) -> Dict[str, Any]:
        self._ensure_worker_running()
        return {
            "enabled": str(self.settings.enabled).lower(),
            "running": str(bool(self._worker and self._worker.is_alive())).lower(),
            "local_timezone": self.settings.local_timezone,
            "refresh_interval_seconds": str(self.settings.near_term_refresh_interval_seconds),
            "calendar_sync_interval_seconds": str(self.settings.calendar_sync_interval_seconds),
            "near_term_refresh_interval_seconds": str(self.settings.near_term_refresh_interval_seconds),
            "release_watch_interval_seconds": str(self.settings.release_watch_interval_seconds),
            "near_term_window_hours": str(self.settings.near_term_window_hours),
            "release_watch_lookback_minutes": str(self.settings.release_watch_lookback_minutes),
            "release_watch_lookahead_minutes": str(self.settings.release_watch_lookahead_minutes),
            "last_refresh_at": self._last_refresh_at.isoformat() if self._last_refresh_at else None,
            "last_refresh_started_at": self._last_refresh_started_at.isoformat() if self._last_refresh_started_at else None,
            "last_refresh_completed_at": self._last_refresh_completed_at.isoformat() if self._last_refresh_completed_at else None,
            "last_refresh_error": self._last_refresh_error,
            "last_refresh_status": (self._last_refresh_summary or {}).get("status") if self._last_refresh_summary else None,
            "refresh_in_progress": str(self._refresh_in_progress).lower(),
            "last_refresh_duration_ms": str(self._last_refresh_duration_ms) if self._last_refresh_duration_ms is not None else None,
            "consecutive_failures": str(self._consecutive_failures),
            "stale": str(self.is_stale()).lower(),
            "default_countries": ",".join(self.settings.default_countries),
            "provider_status": self._provider_status,
            "job_status": {
                job_type: {
                    **state,
                    "next_run_at": self._next_run_at[job_type].isoformat() if self._next_run_at[job_type] else None,
                    "interval_seconds": str(self._job_interval(job_type)),
                }
                for job_type, state in self._job_state.items()
            },
        }
