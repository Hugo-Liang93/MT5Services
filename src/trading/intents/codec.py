from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from src.signals.models import SignalEvent


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return normalized.isoformat()


def _deserialize_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def signal_event_to_payload(event: SignalEvent) -> dict[str, Any]:
    payload = asdict(event)
    payload["generated_at"] = _serialize_datetime(event.generated_at)
    payload["parent_bar_time"] = _serialize_datetime(event.parent_bar_time)
    return payload


def signal_event_from_payload(payload: dict[str, Any]) -> SignalEvent:
    return SignalEvent(
        symbol=str(payload.get("symbol") or ""),
        timeframe=str(payload.get("timeframe") or ""),
        strategy=str(payload.get("strategy") or ""),
        direction=str(payload.get("direction") or ""),
        confidence=float(payload.get("confidence") or 0.0),
        signal_state=str(payload.get("signal_state") or ""),
        scope=str(payload.get("scope") or "confirmed"),
        indicators=dict(payload.get("indicators") or {}),
        metadata=dict(payload.get("metadata") or {}),
        generated_at=_deserialize_datetime(payload.get("generated_at"))
        or datetime.now(timezone.utc),
        signal_id=str(payload.get("signal_id") or ""),
        reason=str(payload.get("reason") or ""),
        parent_bar_time=_deserialize_datetime(payload.get("parent_bar_time")),
    )
