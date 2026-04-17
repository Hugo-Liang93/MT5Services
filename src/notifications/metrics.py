"""Lightweight counters for notification module observability.

Thread-safe counter object that HealthMonitor can sample periodically. We
intentionally do not reach for prometheus_client or similar — the rest of
MT5Services uses simple counter dicts and these match that style.

Counters reset on process restart; long-term trends belong in the SQLite
outbox / DLQ counts (which survive restart).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Mapping


@dataclass
class NotificationMetrics:
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _counters: dict[str, int] = field(default_factory=dict)

    def incr(self, key: str, delta: int = 1) -> None:
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + int(delta)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)

    # Known counter names (single source of truth to avoid key-typo drift).
    CLASSIFIED = "classified_total"
    SUPPRESSED_DEDUP = "suppressed_dedup_total"
    DROPPED_RATE_LIMIT = "dropped_rate_limit_total"
    ENQUEUED_OUTBOX = "enqueued_outbox_total"
    SEND_SUCCESS = "send_success_total"
    SEND_FAILED_RETRYABLE = "send_failed_retryable_total"
    SEND_FAILED_TERMINAL = "send_failed_terminal_total"
    MOVED_TO_DLQ = "moved_to_dlq_total"
    WORKER_LOOPS = "worker_loops_total"
    RENDER_FAILURES = "render_failures_total"
    ENQUEUE_DUPLICATES = "enqueue_duplicates_total"


def key_with_severity(base: str, severity: str | Mapping[str, str]) -> str:
    """Helper for severity-scoped counters (e.g. ``send_success_total::critical``)."""
    if isinstance(severity, Mapping):
        severity_value = str(severity.get("severity", "unknown"))
    else:
        severity_value = str(severity)
    return f"{base}::{severity_value}"
