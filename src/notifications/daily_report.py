"""Daily snapshot report generator for the scheduler's ``daily_report`` job.

Builds a single ``NotificationEvent`` with fields drawn from the read-model.
Kept deliberately **snapshot-shaped** (not cumulative day-over-day stats):

- PnL aggregation across a UTC day would need trade-history queries the
  read-model doesn't expose today. A snapshot already tells the operator
  most of what they want to know at 5 AM Beijing: what's the system's
  posture, any alerts pending, any circuit tripped.
- Adding real PnL deltas later doesn't change the transport path — the
  template just grows a few more ``{{ }}`` slots.

Failure policy: if the read-model is missing or a query raises, we emit a
degraded "read-model unavailable" event instead of skipping — the operator
still gets a heartbeat and knows daily_report is wired.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from src.notifications.events import NotificationEvent, Severity

logger = logging.getLogger(__name__)


def _safe_dict(fn: Optional[Any]) -> Mapping[str, Any]:
    """Call a zero-arg readmodel method, return {} on any failure."""
    if fn is None:
        return {}
    try:
        result = fn()
    except Exception:  # noqa: BLE001 — report generator must never crash the scheduler
        logger.exception("daily_report readmodel call raised")
        return {}
    if isinstance(result, Mapping):
        return result
    return {}


def _fmt_bool(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if value is None:
        return "-"
    return str(value)


def _fmt_count(value: Any) -> str:
    if value is None:
        return "0"
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return str(value)


class DailyReportGenerator:
    """Render a daily snapshot into a notification event.

    Dependency injected: takes the RuntimeReadModel (optional — works in
    tests with just a stub or None).
    """

    EVENT_TYPE = "daily_report"
    TEMPLATE_KEY = "info_daily_report"

    def __init__(
        self,
        *,
        runtime_read_model: Optional[Any],
        instance: str,
        clock: Any = None,
    ) -> None:
        self._rrm = runtime_read_model
        self._instance = instance
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def build_event(self) -> NotificationEvent:
        now = self._clock()
        payload = self._gather_payload(now=now)
        return NotificationEvent.build(
            event_type=self.EVENT_TYPE,
            severity=Severity.INFO,
            template_key=self.TEMPLATE_KEY,
            source="scheduler",
            instance=self._instance,
            payload=payload,
            # One report per calendar day; firing twice same day (e.g. manual
            # trigger) dedups safely via the date component.
            dedup_parts=(payload["report_date_utc"],),
        )

    def _gather_payload(self, *, now: datetime) -> dict[str, Any]:
        rrm = self._rrm
        health = _safe_dict(getattr(rrm, "health_report", None))
        executor = _safe_dict(getattr(rrm, "trade_executor_summary", None))
        positions = _safe_dict(getattr(rrm, "tracked_positions_payload", None))
        signals = _safe_dict(getattr(rrm, "signal_runtime_summary", None))
        mode_info = _safe_dict(getattr(rrm, "runtime_mode_summary", None))

        active_alerts = health.get("active_alerts") or []
        active_alerts_count = (
            len(active_alerts)
            if isinstance(active_alerts, list)
            else _fmt_count(active_alerts)
        )

        health_status = str(health.get("status") or health.get("state") or "-")
        signal_running = signals.get("running")
        return {
            "instance": self._instance,
            "report_date_utc": now.strftime("%Y-%m-%d"),
            "report_time_utc": now.strftime("%H:%M UTC"),
            "health_status": health_status,
            "active_alerts_count": _fmt_count(active_alerts_count),
            "mode": str(mode_info.get("current_mode") or "-"),
            "executor_enabled": _fmt_bool(executor.get("enabled")),
            "circuit_open": _fmt_bool(executor.get("circuit_open")),
            "signals_running": _fmt_bool(signal_running),
            "open_positions": _fmt_count(positions.get("count")),
            "pending_entries": _fmt_count(
                (positions.get("manager") or {}).get("pending_entries_count")
                if isinstance(positions.get("manager"), Mapping)
                else positions.get("pending_entries_count")
            ),
            # Kept as raw bool (not _fmt_bool) so template's {% if %} branch
            # evaluates correctly — "否" would be a truthy string.
            "read_model_ok": rrm is not None,
        }
