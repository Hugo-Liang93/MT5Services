from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional


def get_updates(
    service,
    start_time: Optional[datetime],
    end_time: Optional[datetime],
    limit: int = 500,
    event_uid: Optional[str] = None,
    snapshot_reasons: Optional[List[str]] = None,
    job_types: Optional[List[str]] = None,
) -> List[Dict[str, object]]:
    rows = service.db.fetch_economic_calendar_updates(
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


def stats(service) -> Dict[str, object]:
    service._ensure_worker_running()
    staleness_seconds = service.staleness_seconds()
    return {
        "enabled": str(service.settings.enabled).lower(),
        "running": str(bool(service._worker and service._worker.is_alive())).lower(),
        "health_state": service.health_state(),
        "warming_up": str(service.is_warming_up()).lower(),
        "local_timezone": service.settings.local_timezone,
        "refresh_interval_seconds": str(service.settings.near_term_refresh_interval_seconds),
        "calendar_sync_interval_seconds": str(service.settings.calendar_sync_interval_seconds),
        "near_term_refresh_interval_seconds": str(service.settings.near_term_refresh_interval_seconds),
        "release_watch_interval_seconds": str(service.settings.release_watch_interval_seconds),
        "near_term_window_hours": str(service.settings.near_term_window_hours),
        "release_watch_lookback_minutes": str(service.settings.release_watch_lookback_minutes),
        "release_watch_lookahead_minutes": str(service.settings.release_watch_lookahead_minutes),
        "bootstrap_started_at": service._scheduler_started_at.isoformat() if service._scheduler_started_at else None,
        "bootstrap_deadline_at": service._bootstrap_deadline_at.isoformat() if service._bootstrap_deadline_at else None,
        "last_refresh_at": service._last_refresh_at.isoformat() if service._last_refresh_at else None,
        "last_refresh_started_at": service._last_refresh_started_at.isoformat() if service._last_refresh_started_at else None,
        "last_refresh_completed_at": service._last_refresh_completed_at.isoformat() if service._last_refresh_completed_at else None,
        "last_refresh_error": service._last_refresh_error,
        "last_refresh_status": (service._last_refresh_summary or {}).get("status") if service._last_refresh_summary else None,
        "refresh_in_progress": str(service._refresh_in_progress).lower(),
        "last_refresh_duration_ms": str(service._last_refresh_duration_ms) if service._last_refresh_duration_ms is not None else None,
        "consecutive_failures": str(service._consecutive_failures),
        "staleness_seconds": str(staleness_seconds) if staleness_seconds is not None else None,
        "stale": str(service.is_stale()).lower(),
        "default_countries": ",".join(service.settings.default_countries),
        "provider_status": service._provider_status,
        "job_status": {
            job_type: {
                **state,
                "next_run_at": service._next_run_at[job_type].isoformat() if service._next_run_at[job_type] else None,
                "interval_seconds": str(service._job_interval(job_type)),
            }
            for job_type, state in service._job_state.items()
        },
    }
