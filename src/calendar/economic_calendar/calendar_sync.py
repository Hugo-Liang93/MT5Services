from __future__ import annotations

import logging
import random
import time
from datetime import date, datetime, timedelta
from threading import Thread
from typing import Any, Dict, List, Optional, Sequence

from src.clients.economic_calendar import EconomicCalendarError, EconomicCalendarEvent
from src.monitoring.runtime_task_status import RuntimeTaskState

logger = logging.getLogger(__name__)

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
    from src.calendar.service import _utc_now as _service_utc_now

    return _service_utc_now()


def _start_of_day(value: date) -> datetime:
    from src.calendar.service import _start_of_day as _service_start_of_day

    return _service_start_of_day(value)


def _end_of_day(value: date) -> datetime:
    from src.calendar.service import _end_of_day as _service_end_of_day

    return _service_end_of_day(value)


def fetch_existing_by_uid(service, events: Sequence[EconomicCalendarEvent]) -> Dict[str, EconomicCalendarEvent]:
    event_uids = sorted({event.event_uid for event in events})
    rows = service.db.fetch_economic_calendar_by_uids(event_uids)
    return {event.event_uid: event for event in (EconomicCalendarEvent.from_db_row(row) for row in rows)}


def store_with_backpressure_control(service, channel: str, rows: List[tuple]) -> None:
    if not rows:
        return
    if service.storage_writer is None:
        getattr(service.db, _DIRECT_WRITE_CHANNELS[channel])(rows)
        return
    batch_size = service.storage_writer.get_channel_batch_size(channel) or len(rows)
    if len(rows) >= max(50, batch_size):
        service.storage_writer.write_now(channel, rows)
        return
    for row in rows:
        service.storage_writer.enqueue(channel, row)


def write_events(
    service,
    job_type: str,
    events: List[EconomicCalendarEvent],
    *,
    value_check: bool,
) -> Dict[str, int]:
    if not events:
        return {"written": 0, "snapshots_written": 0, "deleted": 0}
    observed_at = _utc_now()
    existing_map = fetch_existing_by_uid(service, events)
    rows: List[tuple] = []
    update_rows: List[tuple] = []
    delete_keys: List[tuple[datetime, str]] = []

    newly_released: List[EconomicCalendarEvent] = []
    for event in events:
        existing = existing_map.get(event.event_uid)
        prepared = service._apply_lifecycle(event, existing, observed_at, value_check=value_check)
        if existing and existing.scheduled_at != prepared.scheduled_at:
            delete_keys.append((existing.scheduled_at, existing.event_uid))
        reason = service._snapshot_reason(existing, prepared)
        rows.append(prepared.to_row())
        if reason is not None:
            update_rows.append(
                prepared.to_update_row(
                    recorded_at=observed_at,
                    snapshot_reason=reason,
                    job_type=job_type,
                )
            )
        # 检测状态变为 released 的事件
        if (
            prepared.status == "released"
            and (existing is None or existing.status != "released")
        ):
            newly_released.append(prepared)

    with service._lock:
        if delete_keys:
            service.db.delete_economic_calendar_by_keys(delete_keys)
        if service.storage_writer is None:
            service.db.write_economic_calendar(rows)
            if update_rows:
                service.db.write_economic_calendar_updates(update_rows)
        else:
            store_with_backpressure_control(service, "economic_calendar", rows)
            store_with_backpressure_control(service, "economic_calendar_updates", update_rows)

    # 通知 MarketImpactAnalyzer 有新 released 事件
    analyzer = getattr(service, "market_impact_analyzer", None)
    if analyzer is not None and newly_released:
        for released_event in newly_released:
            try:
                analyzer.on_event_released(released_event)
            except Exception:
                logger.exception(
                    "MarketImpactAnalyzer callback failed for %s",
                    released_event.event_uid,
                )

    return {
        "written": len(rows),
        "snapshots_written": len(update_rows),
        "deleted": len(delete_keys),
    }


def fetch_job_events(
    service,
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
    for provider in service._normalize_sources(sources, job_type):
        provider_obj = service.registry.get(provider)
        if provider_obj is None or not provider_obj.is_configured():
            continue
        try:
            fetched = service._fetch_from_provider(provider, start_at.date(), end_at.date(), countries)
            provider_counts[provider] = len(fetched)
            events.extend(fetched)
        except Exception as exc:
            provider_errors[provider] = str(exc)
    if provider_errors and not provider_counts:
        raise EconomicCalendarError(f"All economic calendar providers failed: {provider_errors}")

    enriched_events = service._enrich_events(events)
    filtered = [event for event in enriched_events if service._event_within_bounds(event, start_at, end_at)]
    deduped = {event.event_uid: event for event in sorted(filtered, key=lambda item: item.scheduled_at)}
    return list(deduped.values()), provider_counts, provider_errors


def job_summary(
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


def schedule_next_run(service, job_type: str, *, from_time: Optional[datetime] = None) -> None:
    if job_type == "release_watch":
        # 三档自适应：根据最近事件动态计算下次轮询间隔
        base_interval = service._job_interval(job_type)
        if base_interval <= 0:
            service._next_run_at[job_type] = None
            return
        try:
            interval = service.compute_release_watch_interval()
        except Exception as exc:
            logger.warning("compute_release_watch_interval() failed, using idle: %s", exc)
            interval = float(service.settings.release_watch_idle_interval_seconds)
        service._next_run_at[job_type] = (from_time or _utc_now()) + timedelta(seconds=interval)
        return
    interval = service._job_interval(job_type)
    service._next_run_at[job_type] = None if interval <= 0 else (from_time or _utc_now()) + timedelta(seconds=interval)


def runtime_task_details(service, job_type: str) -> Dict[str, Any]:
    job_state = service._job_state[job_type]
    return {
        "enabled": bool(job_state["enabled"]),
        "interval_seconds": service._job_interval(job_type),
        "last_result_status": job_state.get("last_result_status"),
        "last_fetched": int(job_state["last_fetched"]),
        "last_written": int(job_state["last_written"]),
        "last_snapshots": int(job_state["last_snapshots"]),
        "provider_status": service._provider_status,
    }


def runtime_task_row(service, job_type: str) -> tuple:
    job_state = service._job_state[job_type]
    updated_at = _utc_now()
    started_at = datetime.fromisoformat(job_state["last_started_at"]) if job_state.get("last_started_at") else None
    completed_at = (
        datetime.fromisoformat(job_state["last_completed_at"])
        if job_state.get("last_completed_at")
        else None
    )
    next_run_at = service._next_run_at.get(job_type)
    # §0dj：runtime_identity 必填——calendar_sync service 装配契约要求显式
    # 注入 runtime_identity，不再 fallback 到 legacy_instance_id 字面量。
    runtime_identity = service._runtime_identity
    if runtime_identity is None:
        raise RuntimeError(
            "calendar_sync runtime_task_row requires service._runtime_identity; "
            "装配层必须在 EconomicCalendarService 构造时显式注入"
        )
    return (
        _RUNTIME_COMPONENT,
        job_type,
        updated_at,
        str(
            job_state.get("last_status")
            or (
                RuntimeTaskState.IDLE.value
                if job_state["enabled"]
                else RuntimeTaskState.DISABLED.value
            )
        ),
        started_at,
        completed_at,
        next_run_at,
        job_state.get("last_duration_ms"),
        int(job_state.get("success_count", 0)),
        int(job_state.get("failure_count", 0)),
        int(job_state.get("consecutive_failures", 0)),
        job_state.get("last_error"),
        runtime_task_details(service, job_type),
        runtime_identity.instance_id,
        runtime_identity.instance_role,
        runtime_identity.account_key,
        runtime_identity.account_alias,
    )


def persist_job_state(service, job_type: str) -> None:
    try:
        service.db.write_runtime_task_status([runtime_task_row(service, job_type)])
    except Exception as exc:  # pragma: no cover
        logger.debug("Failed to persist economic runtime task status for %s: %s", job_type, exc)


def update_job_state(
    service,
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
    job_state = service._job_state[job_type]
    job_state["last_started_at"] = started_at.isoformat()
    job_state["last_completed_at"] = completed_at.isoformat()
    job_state["last_error"] = error
    job_state["last_status"] = status
    job_state["last_result_status"] = status
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
    schedule_next_run(service, job_type, from_time=completed_at)
    persist_job_state(service, job_type)


def restore_job_state(service) -> None:
    runtime_identity = getattr(service, "_runtime_identity", None)
    try:
        # §0y P2：runtime_task_status 表 PK 是 (instance_id, component, task_name)，
        # 旧实现只按 component + instance_role + account_key 取行 →
        # 滚动重启 / 双开实例 / 同账户重复进程下，会把别人的 next_run_at +
        # 失败计数 + 上次刷新时间静默串行恢复到本实例。必须按 instance_id 过滤。
        rows = service.db.fetch_runtime_task_status(
            component=_RUNTIME_COMPONENT,
            instance_id=(
                runtime_identity.instance_id if runtime_identity is not None else None
            ),
            instance_role=(
                runtime_identity.instance_role if runtime_identity is not None else None
            ),
            account_key=(
                runtime_identity.account_key if runtime_identity is not None else None
            ),
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("Failed to restore economic runtime task state: %s", exc)
        return
    latest_success: Optional[Dict[str, Any]] = None
    latest_attempt: Optional[Dict[str, Any]] = None
    max_consecutive_failures = 0
    for row in rows:
        _, task_name, _, state, started_at, completed_at, next_run_at, duration_ms, success_count, failure_count, consecutive_failures, last_error, details = row
        if task_name not in service._job_state:
            continue
        job_state = service._job_state[task_name]
        job_state["last_status"] = state
        job_state["last_started_at"] = started_at.isoformat() if started_at else None
        job_state["last_completed_at"] = completed_at.isoformat() if completed_at else None
        job_state["last_duration_ms"] = duration_ms
        job_state["success_count"] = int(success_count or 0)
        job_state["failure_count"] = int(failure_count or 0)
        job_state["consecutive_failures"] = int(consecutive_failures or 0)
        job_state["last_error"] = last_error
        max_consecutive_failures = max(max_consecutive_failures, job_state["consecutive_failures"])
        if isinstance(details, dict):
            effective_result_status = details.get("last_result_status") or state
            job_state["last_result_status"] = effective_result_status
            job_state["last_fetched"] = int(details.get("last_fetched", job_state["last_fetched"]))
            job_state["last_written"] = int(details.get("last_written", job_state["last_written"]))
            job_state["last_snapshots"] = int(details.get("last_snapshots", job_state["last_snapshots"]))
        else:
            effective_result_status = state
        if next_run_at is not None:
            service._next_run_at[task_name] = next_run_at
        if completed_at is not None:
            candidate = {
                "job_type": task_name,
                "status": effective_result_status,
                "started_at": started_at,
                "completed_at": completed_at,
                "duration_ms": duration_ms,
                "last_error": last_error,
            }
            if latest_attempt is None or completed_at > latest_attempt["completed_at"]:
                latest_attempt = candidate
            if effective_result_status in {
                RuntimeTaskState.OK.value,
                RuntimeTaskState.PARTIAL.value,
                RuntimeTaskState.COMPLETED.value,
                RuntimeTaskState.READY.value,
                RuntimeTaskState.IDLE.value,
            } or int(success_count or 0) > 0:
                if latest_success is None or completed_at > latest_success["completed_at"]:
                    latest_success = candidate
    service._consecutive_failures = max_consecutive_failures
    if latest_success is not None:
        service._last_refresh_at = latest_success["completed_at"]
    if latest_attempt is not None:
        service._last_refresh_started_at = latest_attempt["started_at"]
        service._last_refresh_completed_at = latest_attempt["completed_at"]
        service._last_refresh_duration_ms = latest_attempt["duration_ms"]
        service._last_refresh_error = latest_attempt["last_error"]
        service._last_refresh_summary = {
            "job_type": latest_attempt["job_type"],
            "status": latest_attempt["status"],
        }


def startup_schedule_time(service, job_type: str, now: datetime) -> Optional[datetime]:
    interval = service._job_interval(job_type)
    if interval <= 0:
        return None
    jitter = min(max(0.0, float(service.settings.refresh_jitter_seconds)), max(0.0, interval))
    delay_seconds = interval
    if job_type == "calendar_sync" and service.settings.startup_refresh:
        delay_seconds = max(0.0, float(service.settings.startup_calendar_sync_delay_seconds))
    scheduled_at = now + timedelta(seconds=delay_seconds)
    if jitter > 0:
        scheduled_at += timedelta(seconds=random.uniform(0.0, jitter))
    return scheduled_at


def run_job(
    service,
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
        events, provider_counts, provider_errors = fetch_job_events(
            service,
            job_type=job_type,
            start_at=start_at,
            end_at=end_at,
            countries=countries or service.settings.default_countries or None,
            sources=sources,
        )
        write_result = write_events(
            service,
            job_type,
            sorted(events, key=lambda item: item.scheduled_at),
            value_check=job_type in {"near_term_sync", "release_watch"},
        )
        completed_at = _utc_now()
        duration_ms = int((time.monotonic() - fetch_started) * 1000)
        summary = job_summary(
            job_type=job_type,
            status=(
                RuntimeTaskState.OK.value
                if not provider_errors
                else RuntimeTaskState.PARTIAL.value
            ),
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
        service._last_refresh_at = completed_at
        service._last_refresh_started_at = started_at
        service._last_refresh_completed_at = completed_at
        service._last_refresh_duration_ms = duration_ms
        service._last_refresh_error = None if not provider_errors else str(provider_errors)
        service._last_refresh_summary = summary
        service._consecutive_failures = 0
        update_job_state(
            service,
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
        logger.info(
            "Economic calendar %s completed: status=%s fetched=%s written=%s snapshots=%s deleted=%s duration_ms=%s provider_counts=%s provider_errors=%s",
            job_type,
            summary["status"],
            summary["fetched"],
            summary["written"],
            summary["snapshots_written"],
            summary["deleted"],
            summary["duration_ms"],
            provider_counts,
            provider_errors or {},
        )
        return summary
    except Exception as exc:
        completed_at = _utc_now()
        duration_ms = int((time.monotonic() - fetch_started) * 1000)
        service._last_refresh_started_at = started_at
        service._last_refresh_completed_at = completed_at
        service._last_refresh_duration_ms = duration_ms
        service._last_refresh_error = str(exc)
        service._consecutive_failures += 1
        update_job_state(
            service,
            job_type,
            started_at=started_at,
            completed_at=completed_at,
            status=RuntimeTaskState.ERROR.value,
            fetched=0,
            written=0,
            snapshots_written=0,
            duration_ms=duration_ms,
            error=str(exc),
        )
        raise


def refresh_service(
    service,
    start_date: Optional[date | datetime] = None,
    end_date: Optional[date | datetime] = None,
    countries: Optional[List[str]] = None,
    sources: Optional[List[str]] = None,
    job_type: str = "calendar_sync",
) -> Dict[str, Any]:
    job_type = _JOB_LABELS.get(job_type, job_type)
    if job_type not in _JOB_LABELS:
        raise EconomicCalendarError(f"Unsupported economic job type: {job_type}")
    if not service.settings.enabled:
        return {
            "status": RuntimeTaskState.DISABLED.value,
            "job_type": job_type,
            "written": 0,
            "fetched": 0,
            "snapshots_written": 0,
        }
    if not service._refresh_lock.acquire(blocking=False):
        return {
            "status": "refresh_in_progress",
            "job_type": job_type,
            "written": 0,
            "fetched": 0,
            "snapshots_written": 0,
        }
    service._refresh_in_progress = True
    try:
        default_start_at, default_end_at = service._job_window(job_type)
        start_at = service._coerce_datetime(start_date) if start_date is not None else default_start_at
        end_at = service._coerce_datetime(end_date) if end_date is not None else default_end_at
        if start_at is None or end_at is None:
            raise EconomicCalendarError("Economic calendar refresh window is invalid")
        if isinstance(start_date, date) and not isinstance(start_date, datetime):
            start_at = _start_of_day(start_date)
        if isinstance(end_date, date) and not isinstance(end_date, datetime):
            end_at = _end_of_day(end_date)
        return run_job(service, job_type=job_type, start_at=start_at, end_at=end_at, countries=countries, sources=sources)
    finally:
        service._refresh_in_progress = False
        service._refresh_lock.release()


def safe_run_job(service, job_type: str) -> None:
    try:
        refresh_service(service, job_type=job_type)
    except EconomicCalendarError as exc:
        service._last_refresh_error = str(exc)
        logger.warning("Economic calendar %s failed: %s", job_type, exc)
    except Exception as exc:  # pragma: no cover
        service._last_refresh_error = str(exc)
        logger.exception("Unexpected economic calendar %s failure: %s", job_type, exc)


def run_scheduler(service) -> None:
    while True:
        if service._stop_event.is_set():
            break
        now = _utc_now()
        due_jobs = [
            job_type
            for job_type, next_run in service._next_run_at.items()
            if next_run is not None and next_run <= now and service._job_interval(job_type) > 0
        ]
        if due_jobs:
            for job_type in sorted(due_jobs, key=service._job_interval):
                safe_run_job(service, job_type)
            # 每轮任务完成后推进 market impact 收集
            analyzer = getattr(service, "market_impact_analyzer", None)
            if analyzer is not None:
                try:
                    analyzer.tick()
                except Exception:
                    logger.exception("MarketImpactAnalyzer.tick() failed")
            continue
        upcoming = [next_run for next_run in service._next_run_at.values() if next_run is not None]
        wait_seconds = 5.0
        if upcoming:
            wait_seconds = max(1.0, min(5.0, min((next_run - now).total_seconds() for next_run in upcoming)))
        if service._stop_event.wait(wait_seconds):
            break


def ensure_worker_running(service) -> None:
    if not service.settings.enabled:
        return
    if not any(service._job_interval(job_type) > 0 for job_type in _JOB_LABELS):
        return
    if service._worker and service._worker.is_alive():
        return
    start_service(service)


def start_service(service) -> None:
    if not service.settings.enabled:
        logger.info("Economic calendar service is disabled")
        return
    restore_job_state(service)
    if not any(service._job_interval(job_type) > 0 for job_type in _JOB_LABELS):
        logger.info("Economic calendar background refresh disabled by interval <= 0")
        return
    if service._worker and service._worker.is_alive():
        return
    service._stop_event.clear()
    now = _utc_now()
    service._scheduler_started_at = now
    startup_immediate_jobs = (
        {"near_term_sync", "release_watch"} if service.settings.startup_refresh else set()
    )
    startup_schedule: Dict[str, Optional[datetime]] = {}
    for job_type in _JOB_LABELS:
        interval = service._job_interval(job_type)
        scheduled_at = startup_schedule_time(service, job_type, now)
        startup_schedule[job_type] = scheduled_at
        restored_next_run = service._next_run_at.get(job_type)
        if interval <= 0:
            service._next_run_at[job_type] = None
        elif job_type in startup_immediate_jobs:
            service._next_run_at[job_type] = now
        elif restored_next_run is None or restored_next_run <= now:
            service._next_run_at[job_type] = scheduled_at
        elif job_type == "calendar_sync" and service.settings.startup_refresh and scheduled_at is not None:
            service._next_run_at[job_type] = max(restored_next_run, scheduled_at)
        persist_job_state(service, job_type)
    bootstrap_anchor = None
    if service._last_refresh_at is None:
        if service._job_interval("calendar_sync") > 0:
            bootstrap_anchor = startup_schedule.get("calendar_sync") or now
        else:
            enabled_startups = [
                startup_schedule.get(job_type) or now
                for job_type in _JOB_LABELS
                if service._job_interval(job_type) > 0
            ]
            bootstrap_anchor = min(enabled_startups) if enabled_startups else None
    service._bootstrap_deadline_at = (
        bootstrap_anchor + timedelta(seconds=service._bootstrap_grace_seconds())
        if bootstrap_anchor is not None
        else None
    )
    service._worker = Thread(
        target=lambda: run_scheduler(service),
        name="economic-calendar-refresh",
        daemon=True,
    )
    service._worker.start()
    logger.info(
        "Economic calendar scheduler started: calendar=%ss startup_delay=%ss near_term=%ss release_watch=%ss local_timezone=%s",
        service.settings.calendar_sync_interval_seconds,
        service.settings.startup_calendar_sync_delay_seconds,
        service.settings.near_term_refresh_interval_seconds,
        service.settings.release_watch_interval_seconds,
        service.settings.local_timezone,
    )


def stop_service(service) -> None:
    """优雅停止 calendar worker；ADR-005 + §0z P2：仅在线程真停下才标 STOPPED。

    旧实现 join(timeout) 后无条件把每个 job 标 STOPPED → 把"线程卡死未退出"
    的高风险 shutdown 漂白成正常停止。新实现：worker 仍 is_alive 时 (a) 保留
    线程引用 (b) 不改 job state（保持原 RUNNING）+ 记录 last_error 反映真相
    (c) 不持久化（避免覆盖正确状态成虚假 STOPPED）。
    """
    service._stop_event.set()
    if service._worker and service._worker.is_alive():
        service._worker.join(timeout=5.0)

    worker_still_alive = (
        service._worker is not None and service._worker.is_alive()
    )
    if worker_still_alive:
        logger.error(
            "Economic calendar worker still alive after 5s join timeout; "
            "preserving thread reference per ADR-005. Job state NOT marked "
            "STOPPED to reflect ground truth (worker did not actually stop)."
        )
        # 不清 _worker、不改 job state、不持久化——避免漂白成虚假 STOPPED
        return

    # worker 真停下：清引用 + 标 STOPPED + 持久化
    if service._worker is not None:
        service._worker = None
    for job_type in _JOB_LABELS:
        service._job_state[job_type]["last_status"] = RuntimeTaskState.STOPPED.value
        persist_job_state(service, job_type)
