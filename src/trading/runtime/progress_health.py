from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def build_progress_health_snapshot(
    *,
    running: bool,
    poll_interval_seconds: float,
    lease_seconds: int,
    last_poll_at: datetime | None,
    last_claim_at: datetime | None,
    last_complete_at: datetime | None,
    last_error: str | None,
    consecutive_errors: int,
    in_flight_id_field: str,
    in_flight_id: str | None,
    in_flight_since: float | None,
    totals: dict[str, Any],
    config: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    wall_now = now or utc_now()
    monotonic_now = time.monotonic()
    poll_stall_threshold = max(float(poll_interval_seconds) * 10.0, 5.0)
    in_flight_stall_threshold = max(float(lease_seconds) * 2.0, poll_stall_threshold)

    stall_reasons: list[str] = []
    if not running:
        stall_reasons.append("not_running")

    poll_age_seconds: float | None = None
    if last_poll_at is None:
        if running:
            stall_reasons.append("never_polled")
    else:
        last_poll = (
            last_poll_at.replace(tzinfo=timezone.utc)
            if last_poll_at.tzinfo is None
            else last_poll_at.astimezone(timezone.utc)
        )
        poll_age_seconds = max(0.0, (wall_now - last_poll).total_seconds())
        if running and poll_age_seconds > poll_stall_threshold:
            stall_reasons.append("poll_stalled")

    if consecutive_errors >= 3:
        stall_reasons.append("consecutive_errors")

    in_flight_duration: float | None = None
    if in_flight_since is not None:
        in_flight_duration = max(0.0, monotonic_now - in_flight_since)
        if in_flight_duration > in_flight_stall_threshold:
            stall_reasons.append("in_flight_stalled")

    payload: dict[str, Any] = {
        "running": bool(running),
        "stalled": bool(stall_reasons),
        "stall_reasons": stall_reasons,
        "last_poll_at": format_dt(last_poll_at),
        "last_claim_at": format_dt(last_claim_at),
        "last_complete_at": format_dt(last_complete_at),
        "last_error": last_error,
        "consecutive_errors": int(consecutive_errors),
        "poll_age_seconds": (
            round(poll_age_seconds, 3) if poll_age_seconds is not None else None
        ),
        "poll_stall_threshold_seconds": round(poll_stall_threshold, 3),
        in_flight_id_field: in_flight_id,
        "in_flight_duration_seconds": (
            round(in_flight_duration, 3) if in_flight_duration is not None else None
        ),
        "in_flight_stall_threshold_seconds": round(in_flight_stall_threshold, 3),
    }
    payload.update(totals)
    payload.update(config)
    return payload
