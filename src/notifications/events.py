"""Core data types for the notification pipeline.

`NotificationEvent` is the unified envelope emitted by all event sources
(PipelineEventBus hooks, HealthMonitor alerts, TradingStateAlerts, scheduler).
Downstream the dispatcher reads `severity` to apply dedup TTL and rate limits,
and `template_key` to select which Markdown template renders the message.

The envelope itself is source-agnostic — classifiers convert each source's raw
payload into this shape before handing off.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


class Severity(str, enum.Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"

    @property
    def tag(self) -> str:
        return self.name


def severity_from_str(value: str | Severity) -> Severity:
    if isinstance(value, Severity):
        return value
    try:
        return Severity(value.strip().lower())
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"invalid severity: {value!r}") from exc


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _freeze_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    return dict(payload)


def build_dedup_key(event_type: str, *parts: str) -> str:
    """Construct a dedup key from an event type + stable identifying parts.

    Empty / None parts are dropped. The result is stable across processes
    for the same input, making it safe to use as an in-memory TTL key.
    """
    pieces = [str(event_type or "").strip()]
    for part in parts:
        text = str(part or "").strip()
        if text:
            pieces.append(text)
    return ":".join(pieces) if pieces[0] else ""


@dataclass(frozen=True)
class NotificationEvent:
    event_type: str
    severity: Severity
    template_key: str
    source: str
    instance: str
    dedup_key: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=_utcnow)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if not self.event_type:
            raise ValueError("event_type is required")
        if not self.template_key:
            raise ValueError("template_key is required")
        if not self.source:
            raise ValueError("source is required")
        if not isinstance(self.severity, Severity):
            raise TypeError(
                f"severity must be Severity enum, got {type(self.severity)}"
            )
        if self.ts.tzinfo is None:
            # Guarantee all timestamps are tz-aware UTC — dedup TTL math depends on it.
            object.__setattr__(self, "ts", self.ts.replace(tzinfo=timezone.utc))

    @classmethod
    def build(
        cls,
        *,
        event_type: str,
        severity: Severity | str,
        template_key: str,
        source: str,
        instance: str,
        payload: Mapping[str, Any] | None = None,
        dedup_parts: tuple[str, ...] = (),
        dedup_key: str | None = None,
        ts: datetime | None = None,
        event_id: str | None = None,
    ) -> "NotificationEvent":
        sev = severity_from_str(severity)
        if dedup_key is not None:
            key = dedup_key.strip()
        else:
            non_empty_parts = tuple(p for p in dedup_parts if str(p or "").strip())
            if not non_empty_parts:
                raise ValueError(
                    "dedup_key must be non-empty (provide at least one dedup_part "
                    "or an explicit dedup_key). Dedup on event_type alone would "
                    "suppress unrelated events of the same category."
                )
            key = build_dedup_key(event_type, *non_empty_parts)
        if not key:
            raise ValueError("dedup_key must be non-empty")
        return cls(
            event_type=event_type,
            severity=sev,
            template_key=template_key,
            source=source,
            instance=instance,
            dedup_key=key,
            payload=_freeze_payload(payload),
            ts=ts if ts is not None else _utcnow(),
            event_id=event_id or uuid.uuid4().hex,
        )

    def with_payload(self, extra: Mapping[str, Any]) -> "NotificationEvent":
        """Return a new event with `extra` merged into payload (non-destructive)."""
        merged = dict(self.payload)
        merged.update(extra)
        return NotificationEvent(
            event_type=self.event_type,
            severity=self.severity,
            template_key=self.template_key,
            source=self.source,
            instance=self.instance,
            dedup_key=self.dedup_key,
            payload=merged,
            ts=self.ts,
            event_id=self.event_id,
        )
