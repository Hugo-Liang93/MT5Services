"""NotificationDispatcher — orchestrates dedup / rate-limit / outbox / transport.

Two responsibilities:
1. **submit()** (called by classifiers/hooks on the source thread): runs dedup
   and rate-limit checks, renders the template, and enqueues into the outbox.
   Fast path; no network I/O.
2. **Worker thread** (owned internally): drains the outbox through the
   transport. Obeys ADR-005 — after ``stop()`` a still-alive thread reference
   is retained to prevent double-consumption on restart.

Operational rules baked in:
- Rate-limit hits for CRITICAL events DO enqueue (will retry later); for
  INFO/WARNING they are dropped and counted. The reasoning: losing an INFO
  duplicate is acceptable noise reduction; losing a CRITICAL is a blind spot.
- ``retry_after_seconds`` from Telegram takes precedence over the local
  backoff ladder for the next attempt.
- Terminal errors (non-retryable) move the row to DLQ on the first failure —
  no wasted retries against a permanent 4xx.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from src.config.models.notifications import NotificationConfig
from src.notifications.dedup import Deduper
from src.notifications.events import NotificationEvent, Severity
from src.notifications.metrics import NotificationMetrics, key_with_severity
from src.notifications.persistence.outbox import (
    OutboxEntry,
    OutboxStore,
    compute_next_retry_at,
)
from src.notifications.rate_limit import RateLimiter
from src.notifications.templates.loader import TemplateRegistry, TemplateValidationError
from src.notifications.transport.base import NotificationTransport

logger = logging.getLogger(__name__)


class NotificationDispatcher:
    def __init__(
        self,
        *,
        config: NotificationConfig,
        templates: TemplateRegistry,
        transport: NotificationTransport,
        outbox: OutboxStore,
        default_chat_id: str,
        deduper: Deduper | None = None,
        rate_limiter: RateLimiter | None = None,
        metrics: NotificationMetrics | None = None,
        worker_poll_interval_seconds: float = 1.0,
        worker_batch_size: int = 10,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not default_chat_id:
            raise ValueError("default_chat_id required")
        self._config = config
        self._templates = templates
        self._transport = transport
        self._outbox = outbox
        self._default_chat_id = default_chat_id
        self._deduper = deduper or Deduper()
        self._rate_limiter = rate_limiter or RateLimiter(
            global_per_minute=config.rate_limit.global_per_minute,
            per_chat_per_minute=config.rate_limit.per_chat_per_minute,
        )
        self._metrics = metrics or NotificationMetrics()
        self._backoff = list(config.runtime.retry_backoff_seconds)
        self._max_attempts = config.runtime.max_retry_attempts
        self._worker_poll_interval = float(worker_poll_interval_seconds)
        self._worker_batch_size = int(worker_batch_size)
        self._clock = clock
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._started = False

    # ── public: submit path (called from bus listeners, hooks, scheduler) ──

    def submit(self, event: NotificationEvent, *, chat_id: str | None = None) -> bool:
        """Enqueue an event for delivery. Returns True if persisted to outbox."""
        target_chat = chat_id or self._default_chat_id
        severity_value = event.severity.value
        self._metrics.incr(NotificationMetrics.CLASSIFIED)
        self._metrics.incr(
            key_with_severity(NotificationMetrics.CLASSIFIED, severity_value)
        )

        ttl = self._config.dedup_ttl(severity_value)  # type: ignore[arg-type]
        dedup_verdict = self._deduper.check(event.dedup_key, ttl_seconds=ttl)
        if not dedup_verdict.allowed:
            self._metrics.incr(NotificationMetrics.SUPPRESSED_DEDUP)
            logger.debug(
                "notification suppressed by dedup: key=%s count=%d",
                event.dedup_key,
                dedup_verdict.suppressed_count,
            )
            return False

        rate_verdict = self._rate_limiter.acquire(target_chat)
        if not rate_verdict.allowed:
            # CRITICAL pushes through anyway (persist to outbox; worker will
            # retry after backoff). INFO / WARNING are silently dropped to
            # avoid unbounded queue buildup during incident storms.
            if event.severity is not Severity.CRITICAL:
                self._metrics.incr(NotificationMetrics.DROPPED_RATE_LIMIT)
                logger.info(
                    "dropped %s notification due to rate limit (%s)",
                    severity_value,
                    rate_verdict.blocked_by,
                )
                return False
            logger.warning(
                "CRITICAL notification rate-limited (%s); enqueuing anyway",
                rate_verdict.blocked_by,
            )

        try:
            rendered = self._templates.render_event(event)
        except (TemplateValidationError, KeyError, ValueError):
            self._metrics.incr(NotificationMetrics.RENDER_FAILURES)
            logger.exception(
                "failed to render notification template for %s",
                event.template_key,
            )
            return False

        try:
            self._outbox.enqueue(
                event_id=event.event_id,
                event_type=event.event_type,
                severity=severity_value,
                chat_id=target_chat,
                rendered_text=rendered,
                dedup_key=event.dedup_key,
                now=self._clock(),
            )
        except ValueError:
            self._metrics.incr(NotificationMetrics.ENQUEUE_DUPLICATES)
            logger.debug("duplicate event_id %s skipped", event.event_id, exc_info=True)
            return False
        self._metrics.incr(NotificationMetrics.ENQUEUED_OUTBOX)
        return True

    # ── public: lifecycle ──

    def start(self) -> None:
        if self._started and self._worker_thread and self._worker_thread.is_alive():
            logger.debug("dispatcher already running")
            return
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._worker_loop,
            name="notification-dispatcher",
            daemon=False,
        )
        thread.start()
        self._worker_thread = thread
        self._started = True

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._worker_thread
        if thread is None:
            return
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning(
                "notification worker thread still alive after %.1fs join; "
                "retaining reference (ADR-005) — will not start a second worker",
                timeout,
            )
            # ADR-005: keep the reference so start() sees is_alive() and refuses.
            return
        self._worker_thread = None

    # ── public: observability ──

    def status(self) -> dict[str, object]:
        outbox_counts = self._outbox.count_by_status()
        return {
            "enabled": self._config.runtime.enabled,
            "worker_running": bool(
                self._worker_thread and self._worker_thread.is_alive()
            ),
            "outbox_counts": outbox_counts,
            "metrics": self._metrics.snapshot(),
            "rate_limit": self._rate_limiter.snapshot(),
            "pending_suppressions": self._deduper.pending_suppressions(),
        }

    # ── internal: worker loop ──

    def drain_once(
        self, *, now: datetime | None = None, batch: int | None = None
    ) -> int:
        """Process one batch of due outbox rows. Returns number attempted.

        Exposed for deterministic testing — real operation is via the worker
        thread started by ``start()``.
        """
        when = now if now is not None else self._clock()
        entries = self._outbox.fetch_due(
            now=when, limit=batch or self._worker_batch_size
        )
        for entry in entries:
            self._deliver(entry, now=when)
        return len(entries)

    def _deliver(self, entry: OutboxEntry, *, now: datetime) -> None:
        result = self._transport.send(chat_id=entry.chat_id, text=entry.rendered_text)
        if result.ok:
            self._outbox.mark_sent(entry.id)
            self._metrics.incr(NotificationMetrics.SEND_SUCCESS)
            self._metrics.incr(
                key_with_severity(NotificationMetrics.SEND_SUCCESS, entry.severity)
            )
            return
        next_attempt = entry.attempt_count + 1
        if not result.retryable or next_attempt >= self._max_attempts:
            reason = "terminal" if not result.retryable else "max_attempts_exceeded"
            error = f"{reason}: {result.error or '?'}"
            self._outbox.mark_dlq(entry.id, error=error)
            self._metrics.incr(NotificationMetrics.MOVED_TO_DLQ)
            self._metrics.incr(NotificationMetrics.SEND_FAILED_TERMINAL)
            logger.error(
                "notification routed to DLQ after %d attempts: %s",
                next_attempt,
                error,
            )
            return
        if result.retry_after_seconds is not None:
            next_retry_at = now + timedelta(seconds=float(result.retry_after_seconds))
        else:
            next_retry_at = compute_next_retry_at(
                attempt_count=entry.attempt_count,
                backoff_seconds=self._backoff,
                now=now,
            )
        self._outbox.mark_failed(
            entry.id, error=str(result.error or "?"), next_retry_at=next_retry_at
        )
        self._metrics.incr(NotificationMetrics.SEND_FAILED_RETRYABLE)
        logger.info(
            "notification send retryable failure (attempt %d/%d), next at %s: %s",
            next_attempt,
            self._max_attempts,
            next_retry_at.isoformat(),
            result.error,
        )

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.drain_once()
                self._metrics.incr(NotificationMetrics.WORKER_LOOPS)
            except Exception:  # noqa: BLE001 — worker must not die
                logger.exception("notification worker iteration failed")
            self._stop_event.wait(self._worker_poll_interval)
