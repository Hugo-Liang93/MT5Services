"""Classify raw source events into unified ``NotificationEvent`` envelopes.

A classifier sits between the event source (PipelineEventBus, HealthMonitor,
TradingStateAlerts) and the dispatcher. It does three things:

1. **Filter by config**: if the event type is mapped to ``off`` (or absent),
   return ``None``. The dispatcher never sees events the operator opted out of.
2. **Extract & reshape payload**: each source carries a different shape of
   data; the classifier produces a flat dict matching the template's
   ``required_vars``. This means template authoring can proceed independently
   of source plumbing.
3. **Attach dedup parts**: stable identifiers (``symbol``, ``strategy``) that
   make dedup keys meaningful per-event-type.

Mapping failures are logged at WARNING and return ``None`` rather than raising —
the classifier runs inside the PipelineEventBus listener callback and a raise
would kill the entire listener chain (see ADR-001).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional

from src.config.models.notifications import NotificationConfig
from src.monitoring.pipeline.event_bus import PipelineEvent
from src.monitoring.pipeline.events import (
    PIPELINE_ADMISSION_REPORT_APPENDED,
    PIPELINE_EXECUTION_BLOCKED,
    PIPELINE_EXECUTION_FAILED,
    PIPELINE_EXECUTION_SUBMITTED,
    PIPELINE_RISK_STATE_CHANGED,
    PIPELINE_UNMANAGED_POSITION_DETECTED,
)
from src.notifications.events import NotificationEvent, severity_from_str

logger = logging.getLogger(__name__)

# Classifier output event types (align with config/notifications.ini [events]).
NOTIF_EXECUTION_FAILED = "execution_failed"
NOTIF_EXECUTION_SUBMITTED = "execution_submitted"
NOTIF_EOD_BLOCK = "eod_block"
NOTIF_UNMANAGED_POSITION = "unmanaged_position"
NOTIF_CIRCUIT_OPEN = "circuit_open"
NOTIF_RISK_REJECTION = "risk_rejection"

_TEMPLATE_KEY = {
    NOTIF_EXECUTION_FAILED: "critical_execution_failed",
    NOTIF_EXECUTION_SUBMITTED: "info_execution_submitted",
    NOTIF_EOD_BLOCK: "warn_eod_block",
    NOTIF_UNMANAGED_POSITION: "critical_unmanaged_position",
    NOTIF_CIRCUIT_OPEN: "critical_circuit_open",
    NOTIF_RISK_REJECTION: "warn_risk_rejection",
}


PipelineMapper = Callable[[PipelineEvent], Optional["_ClassifierOutput"]]


class _ClassifierOutput:
    """Intermediate result before config-driven severity assignment."""

    __slots__ = ("notif_event_type", "payload", "dedup_parts")

    def __init__(
        self,
        *,
        notif_event_type: str,
        payload: Mapping[str, Any],
        dedup_parts: tuple[str, ...],
    ) -> None:
        self.notif_event_type = notif_event_type
        self.payload = dict(payload)
        self.dedup_parts = dedup_parts


def _safe_str(value: Any, default: str = "-") -> str:
    text = str(value or "").strip()
    return text if text else default


def _map_execution_failed(event: PipelineEvent) -> Optional[_ClassifierOutput]:
    payload = event.payload or {}
    return _ClassifierOutput(
        notif_event_type=NOTIF_EXECUTION_FAILED,
        payload={
            "strategy": _safe_str(payload.get("strategy")),
            "symbol": _safe_str(event.symbol),
            "direction": _safe_str(payload.get("direction")),
            "reason": _safe_str(payload.get("reason")),
            "trace_id": _safe_str(event.trace_id),
        },
        dedup_parts=(event.symbol, _safe_str(payload.get("strategy"))),
    )


def _map_execution_submitted(event: PipelineEvent) -> Optional[_ClassifierOutput]:
    payload = event.payload or {}
    return _ClassifierOutput(
        notif_event_type=NOTIF_EXECUTION_SUBMITTED,
        payload={
            "strategy": _safe_str(payload.get("strategy")),
            "symbol": _safe_str(event.symbol),
            "direction": _safe_str(payload.get("direction")),
            "order_kind": _safe_str(payload.get("order_kind")),
            "ticket": _safe_str(payload.get("ticket")),
            "trace_id": _safe_str(event.trace_id),
        },
        # Submitted events are INFO-grade and chatty; dedup per (symbol,strategy,direction)
        # so repeated same-direction entries in TTL are collapsed.
        dedup_parts=(
            event.symbol,
            _safe_str(payload.get("strategy")),
            _safe_str(payload.get("direction")),
        ),
    )


def _map_execution_blocked_eod(event: PipelineEvent) -> Optional[_ClassifierOutput]:
    payload = event.payload or {}
    reason = str(payload.get("reason") or "").lower()
    # Only EOD blocks get their own notification — other block reasons are
    # routed via risk_rejection (admission report) path.
    if "eod" not in reason:
        return None
    return _ClassifierOutput(
        notif_event_type=NOTIF_EOD_BLOCK,
        payload={
            "close_time_utc": (
                _safe_str(payload.get("close_time_utc"))
                if payload.get("close_time_utc")
                else _safe_str(event.ts)
            ),
        },
        # Dedup per calendar day (UTC): first EOD event triggers, rest collapse.
        dedup_parts=(event.ts.split("T", 1)[0] if "T" in event.ts else event.ts,),
    )


def _map_unmanaged_position(event: PipelineEvent) -> Optional[_ClassifierOutput]:
    payload = event.payload or {}
    ticket = _safe_str(payload.get("ticket"))
    return _ClassifierOutput(
        notif_event_type=NOTIF_UNMANAGED_POSITION,
        payload={
            "ticket": ticket,
            "reason": _safe_str(payload.get("reason")),
        },
        # Each unmanaged ticket is significant; dedup only per ticket so that
        # the same handful of tickets don't spam but NEW tickets always surface.
        dedup_parts=(ticket,),
    )


def _map_risk_state_circuit(event: PipelineEvent) -> Optional[_ClassifierOutput]:
    payload = event.payload or {}
    # Only emit when the transition is into an "open" state.
    state = str(payload.get("state") or payload.get("new_state") or "").lower()
    if "circuit_open" not in state and state != "open":
        return None
    return _ClassifierOutput(
        notif_event_type=NOTIF_CIRCUIT_OPEN,
        payload={
            "consecutive_failures": _safe_str(payload.get("consecutive_failures")),
            "last_reason": _safe_str(payload.get("last_reason")),
            "auto_reset_minutes": _safe_str(payload.get("auto_reset_minutes")),
        },
        # One notification per open transition; dedup per occurrence marker (ts bucket).
        dedup_parts=(state,),
    )


def _map_admission_rejection(event: PipelineEvent) -> Optional[_ClassifierOutput]:
    payload = event.payload or {}
    verdict = str(payload.get("verdict") or "").lower()
    if verdict != "reject":
        return None
    reason = _safe_str(payload.get("reason") or payload.get("first_failed_check"))
    return _ClassifierOutput(
        notif_event_type=NOTIF_RISK_REJECTION,
        payload={
            "strategy": _safe_str(payload.get("strategy")),
            "symbol": _safe_str(event.symbol),
            "direction": _safe_str(payload.get("direction")),
            "reason": reason,
        },
        dedup_parts=(event.symbol, _safe_str(payload.get("strategy")), reason),
    )


_PIPELINE_MAPPERS: dict[str, PipelineMapper] = {
    PIPELINE_EXECUTION_FAILED: _map_execution_failed,
    PIPELINE_EXECUTION_SUBMITTED: _map_execution_submitted,
    PIPELINE_EXECUTION_BLOCKED: _map_execution_blocked_eod,
    PIPELINE_UNMANAGED_POSITION_DETECTED: _map_unmanaged_position,
    PIPELINE_RISK_STATE_CHANGED: _map_risk_state_circuit,
    PIPELINE_ADMISSION_REPORT_APPENDED: _map_admission_rejection,
}


class PipelineEventClassifier:
    """Convert PipelineEvent → NotificationEvent (or None if filtered).

    The classifier is a pure function over ``(PipelineEvent, config)`` — it
    holds no mutable state. Safe to call concurrently from multiple bus
    listener threads.
    """

    def __init__(self, *, config: NotificationConfig, instance: str) -> None:
        self._config = config
        self._instance = instance
        self._suppress_info_instances = frozenset(
            config.event_filters.suppress_info_on_instances
        )
        self._allowed_rejection_reasons = frozenset(
            config.event_filters.risk_rejection_reasons
        )

    def classify(self, event: PipelineEvent) -> Optional[NotificationEvent]:
        mapper = _PIPELINE_MAPPERS.get(event.type)
        if mapper is None:
            return None
        try:
            output = mapper(event)
        except Exception:  # noqa: BLE001 — never let a mapper bug kill the listener
            logger.exception(
                "notification classifier mapper raised for event %s", event.type
            )
            return None
        if output is None:
            return None
        severity_value = self._config.severity_for(output.notif_event_type)
        if severity_value == "off":
            return None
        if severity_value == "info" and self._instance in self._suppress_info_instances:
            return None
        # risk_rejection allowlist — only notify on meaningful reject reasons.
        if output.notif_event_type == NOTIF_RISK_REJECTION:
            reason = str(output.payload.get("reason") or "").lower()
            if self._allowed_rejection_reasons and not any(
                allowed.lower() == reason or allowed.lower() in reason
                for allowed in self._allowed_rejection_reasons
            ):
                return None
        template_key = _TEMPLATE_KEY.get(output.notif_event_type)
        if template_key is None:
            logger.warning(
                "no template registered for notif event %s; skipping",
                output.notif_event_type,
            )
            return None
        try:
            return NotificationEvent.build(
                event_type=output.notif_event_type,
                severity=severity_from_str(severity_value),
                template_key=template_key,
                source="pipeline_bus",
                instance=self._instance,
                payload={"instance": self._instance, **output.payload},
                dedup_parts=output.dedup_parts,
            )
        except ValueError:
            logger.exception(
                "failed to build NotificationEvent for %s",
                output.notif_event_type,
            )
            return None
