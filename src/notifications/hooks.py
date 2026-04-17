"""Hooks connecting event sources to the NotificationDispatcher.

Each hook is a thin adapter:
- ``PipelineEventBus`` → ``PipelineEventClassifier.classify`` → dispatcher.
- ``HealthMonitor.set_alert_listener`` → direct NotificationEvent build → dispatcher.

Kept in one file because they share helper shapes and there are only ~2
sources in Phase 1. Split into separate modules when the count exceeds 4.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

from src.monitoring.pipeline.event_bus import PipelineEvent, PipelineEventBus
from src.notifications.classifier import PipelineEventClassifier
from src.notifications.dispatcher import NotificationDispatcher
from src.notifications.events import NotificationEvent, Severity, severity_from_str

logger = logging.getLogger(__name__)


# Map HealthMonitor alert_level strings to notification severity. Anything
# outside this map is ignored (e.g. "ok" recoveries don't produce alerts).
_HEALTH_LEVEL_TO_SEVERITY: dict[str, Severity] = {
    "critical": Severity.CRITICAL,
    "warning": Severity.WARNING,
}


# TradingStateAlerts code → (notification event_type, template_key).
# Only codes present here are converted — unknown codes from new alerts are
# logged but not notified (fail-safe: prefer silence over malformed dispatch).
_TRADING_STATE_CODE_MAP: dict[str, tuple[str, str]] = {
    "pending_missing": ("pending_missing", "critical_pending_missing"),
    # pending_orphan / pending_active_mismatch don't have dedicated templates
    # in Phase 1 — they fold into a generic "trading_state_warning" bucket.
    # See Phase 2 design: warn_trading_state template covers them.
    "pending_orphan": ("trading_state_warning", "warn_trading_state"),
    "pending_active_mismatch": ("trading_state_warning", "warn_trading_state"),
    "unmanaged_live_positions": (
        "unmanaged_position",
        "critical_unmanaged_position",
    ),
}


_SEVERITY_STRING_MAP: dict[str, Severity] = {
    "critical": Severity.CRITICAL,
    "warning": Severity.WARNING,
    "info": Severity.INFO,
}


def make_pipeline_listener(
    *,
    classifier: PipelineEventClassifier,
    dispatcher: NotificationDispatcher,
) -> Callable[[PipelineEvent], None]:
    """Build a PipelineEventBus listener that classifies & submits events.

    Returns a plain callable (not a class) so tests can register it directly
    with any bus-like object.
    """

    def _listener(event: PipelineEvent) -> None:
        try:
            notif = classifier.classify(event)
        except Exception:  # noqa: BLE001 — defensive; classifier already isolates
            logger.exception(
                "pipeline notification listener swallowed classifier exception"
            )
            return
        if notif is None:
            return
        try:
            dispatcher.submit(notif)
        except Exception:  # noqa: BLE001 — listener chain safety (ADR-001)
            logger.exception("pipeline notification listener submit failed")

    return _listener


def register_pipeline_listener(
    bus: PipelineEventBus,
    *,
    classifier: PipelineEventClassifier,
    dispatcher: NotificationDispatcher,
) -> Callable[[PipelineEvent], None]:
    listener = make_pipeline_listener(classifier=classifier, dispatcher=dispatcher)
    bus.add_listener(listener)
    return listener


def make_health_alert_listener(
    *,
    dispatcher: NotificationDispatcher,
    instance: str,
    template_key: str = "warn_health_alert",
    event_type: str = "health_degraded",
) -> Callable[[Mapping[str, Any]], None]:
    """Build a HealthMonitor ``set_alert_listener`` callback.

    The callback converts the flat alert dict produced by HealthMonitor into a
    unified NotificationEvent. dedup is keyed by ``(component, metric_name,
    alert_level)`` so repeated same-level alerts in the TTL window collapse,
    but escalations (warning → critical) send a fresh message.
    """

    def _listener(alert: Mapping[str, Any]) -> None:
        level = str(alert.get("alert_level") or "").lower()
        severity = _HEALTH_LEVEL_TO_SEVERITY.get(level)
        if severity is None:
            return
        component = str(alert.get("component") or "")
        metric_name = str(alert.get("metric_name") or "")
        try:
            notif = NotificationEvent.build(
                event_type=event_type,
                severity=severity,
                template_key=template_key,
                source="health_monitor",
                instance=instance,
                payload={
                    "instance": instance,
                    "metric": (
                        f"{component}.{metric_name}" if component else metric_name
                    ),
                    "level": level,
                    "value": alert.get("value"),
                    "threshold": alert.get("threshold"),
                },
                dedup_parts=(component or "-", metric_name or "-", level),
            )
        except ValueError:
            logger.exception("failed to build health notification event")
            return
        try:
            dispatcher.submit(notif)
        except Exception:  # noqa: BLE001 — caller (record_metric) must stay alive
            logger.exception("health alert notification submit failed")

    return _listener


def severity_from_alert_level(level: str) -> Severity | None:
    """Tiny helper used by tests; returns ``None`` on non-alert levels."""
    try:
        return severity_from_str(level)
    except ValueError:
        return None


def make_trading_state_poll_job(
    *,
    trading_state_alerts: Any,
    dispatcher: NotificationDispatcher,
    instance: str,
) -> Callable[[], None]:
    """Build a scheduler job that polls TradingStateAlerts.summary() and submits
    one NotificationEvent per active alert code.

    Design choice — **poll vs push**:
    TradingStateAlerts has no event stream; its ``summary()`` is a pull-based
    snapshot of DB + broker state. Polling every 60s with dedup TTL (default
    300s for CRITICAL) gives a clean signal: the same unresolved condition
    collapses to one notification per window, while a new ticket or code
    surfaces promptly.

    Only codes in ``_TRADING_STATE_CODE_MAP`` are converted; unknown codes
    are logged (DEBUG) to surface when adapter + alerts class drift.
    """

    def _poll() -> None:
        summary_fn = getattr(trading_state_alerts, "summary", None)
        if not callable(summary_fn):
            logger.debug("trading_state_alerts has no summary(); skipping poll")
            return
        try:
            snapshot = summary_fn()
        except Exception:
            logger.exception("trading_state_alerts.summary() raised")
            return
        alerts = (snapshot or {}).get("alerts") or []
        for alert in alerts:
            _submit_trading_state_alert(
                alert,
                dispatcher=dispatcher,
                instance=instance,
            )

    return _poll


def _submit_trading_state_alert(
    alert: Mapping[str, Any],
    *,
    dispatcher: NotificationDispatcher,
    instance: str,
) -> None:
    code = str(alert.get("code") or "").strip()
    if not code:
        return
    mapping = _TRADING_STATE_CODE_MAP.get(code)
    if mapping is None:
        logger.debug("trading_state alert code not in map: %s", code)
        return
    event_type, template_key = mapping
    severity_str = str(alert.get("severity") or "warning").lower()
    severity = _SEVERITY_STRING_MAP.get(severity_str, Severity.WARNING)
    details = alert.get("details") or {}
    payload: dict[str, Any] = {
        "instance": instance,
        "code": code,
        "message": str(alert.get("message") or ""),
    }
    dedup_parts: tuple[str, ...] = (code,)
    # Per-code payload enrichment so templates render richly.
    if code == "pending_missing":
        payload["missing_count"] = details.get("missing_count", 0)
    elif code == "unmanaged_live_positions":
        tickets = details.get("unmanaged_tickets") or []
        payload["ticket"] = ",".join(str(t) for t in tickets) or "-"
        payload["reason"] = "unmanaged"
        # Dedup per ticket set — a newly-appearing ticket breaks the dedup and
        # surfaces immediately, while the same set recurring collapses.
        dedup_parts = (code, payload["ticket"])
    else:
        # Generic warn_trading_state template: show the message verbatim.
        payload["metric"] = code
        payload["level"] = severity_str
        payload["value"] = details.get("orphan_count") or details.get(
            "persisted_active_count", "-"
        )
        payload["threshold"] = "-"
    try:
        event = NotificationEvent.build(
            event_type=event_type,
            severity=severity,
            template_key=template_key,
            source="trading_state_alerts",
            instance=instance,
            payload=payload,
            dedup_parts=dedup_parts,
        )
    except ValueError:
        logger.exception("failed to build trading_state notification event")
        return
    try:
        dispatcher.submit(event)
    except Exception:
        logger.exception("trading_state alert submit failed")
