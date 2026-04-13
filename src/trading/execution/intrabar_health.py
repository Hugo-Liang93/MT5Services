from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from src.signals.metadata_keys import MetadataKey as MK


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def build_execution_intrabar_health(
    metadata: Mapping[str, Any] | None,
    *,
    scope: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if str(scope or "").strip().lower() != "intrabar":
        return {
            "applicable": False,
            "configured": False,
            "status": "not_applicable",
            "stale": False,
            "age_seconds": None,
            "stale_threshold_seconds": None,
        }

    source = dict(metadata or {})
    synthesis = source.get(MK.INTRABAR_SYNTHESIS)
    if not isinstance(synthesis, Mapping):
        return {
            "applicable": True,
            "configured": False,
            "status": "unavailable",
            "stale": True,
            "age_seconds": None,
            "stale_threshold_seconds": None,
            "error_code": "intrabar_synthesis_missing",
            "trigger_tf": str(source.get(MK.INTRABAR_TRIGGER_TF) or "").strip().upper()
            or None,
        }

    payload = dict(synthesis)
    synthesized_at = _parse_datetime(payload.get("synthesized_at"))
    if synthesized_at is None:
        return {
            "applicable": True,
            "configured": False,
            "status": "unavailable",
            "stale": True,
            "age_seconds": None,
            "stale_threshold_seconds": payload.get("stale_threshold_seconds"),
            "error_code": "intrabar_synthesis_timestamp_missing",
            "trigger_tf": str(payload.get("trigger_tf") or "").strip().upper() or None,
        }

    try:
        stale_threshold_seconds = float(payload.get("stale_threshold_seconds"))
    except (TypeError, ValueError):
        stale_threshold_seconds = None

    reference_now = now or datetime.now(timezone.utc)
    age_seconds = max(
        0.0,
        (
            reference_now.astimezone(timezone.utc)
            - synthesized_at.astimezone(timezone.utc)
        ).total_seconds(),
    )
    stale = bool(
        stale_threshold_seconds is not None and age_seconds > stale_threshold_seconds
    )
    return {
        "applicable": True,
        "configured": True,
        "status": "stale" if stale else "healthy",
        "stale": stale,
        "age_seconds": round(age_seconds, 3),
        "stale_threshold_seconds": stale_threshold_seconds,
        "expected_interval_seconds": payload.get("expected_interval_seconds"),
        "synthesized_at": synthesized_at.isoformat(),
        "trigger_tf": str(payload.get("trigger_tf") or "").strip().upper() or None,
        "last_child_bar_time": payload.get("last_child_bar_time"),
        "child_bar_count": payload.get("child_bar_count"),
        "count": payload.get("count"),
    }
