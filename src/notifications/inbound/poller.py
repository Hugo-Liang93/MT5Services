"""Telegram long-polling worker thread.

Simpler alternative to webhooks: one dedicated thread calls
``getUpdates(offset=N, timeout=30)``, which the Telegram server holds open
for up to ``timeout`` seconds before returning either (a) new updates or
(b) an empty list on timeout. We immediately re-poll.

Offset water-marking:
- The server uses ``offset = last_seen_update_id + 1`` to implicitly
  acknowledge already-seen updates. Once we advance the offset past an
  update_id, the server will never redeliver it.
- The water mark is **in-memory only** (no SQLite for Phase 3). Process
  restart means Telegram will redeliver recent updates — acceptable for
  read-only query commands (re-querying /health is idempotent). For Phase 4
  control commands we'll need a persistent offset so restart doesn't
  re-execute the last halt/resume.

Error handling:
- Network / HTTP errors → exponential backoff inside the loop (up to 30s)
- Malformed getUpdates responses → skip the iteration, log, continue
- Unhandled exceptions in router.dispatch → absorbed by router itself
- ADR-005: on stop() timeout, retain the thread reference to prevent a
  second poller attaching to the same bot.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Mapping, Optional

from src.notifications.inbound.router import CommandRouter
from src.notifications.transport.telegram import TelegramTransport

logger = logging.getLogger(__name__)


_MIN_BACKOFF = 1.0
_MAX_BACKOFF = 30.0


class TelegramPoller:
    """Long-polling inbound worker.

    Owns a single thread and the update_id water mark. Processing is delegated
    entirely to ``router.dispatch`` — this class knows nothing about
    commands or replies.
    """

    def __init__(
        self,
        *,
        transport: TelegramTransport,
        router: CommandRouter,
        timeout_seconds: int = 30,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        self._transport = transport
        self._router = router
        self._timeout_seconds = int(timeout_seconds)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._offset: int = 0
        # Observability counters — surfaced via status().
        self._polls_ok: int = 0
        self._polls_failed: int = 0
        self._updates_processed: int = 0
        self._last_successful_poll_at: Optional[str] = None

    # ── public lifecycle ──

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            logger.debug("poller already running")
            return
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._run, name="telegram-poller", daemon=False
        )
        thread.start()
        self._thread = thread

    def stop(self, *, timeout: float = 35.0) -> None:
        """Signal stop and join. The default 35s timeout is deliberately
        longer than ``timeout_seconds`` because we may be mid-longpoll;
        Telegram returns empty list once timeout elapses, at which point
        the loop checks ``_stop_event``."""
        self._stop_event.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning(
                "poller thread still alive after %.1fs join — retaining reference (ADR-005)",
                timeout,
            )
            return
        self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        return {
            "running": self.is_running(),
            "current_offset": self._offset,
            "polls_ok": self._polls_ok,
            "polls_failed": self._polls_failed,
            "updates_processed": self._updates_processed,
            "last_successful_poll_at": self._last_successful_poll_at,
        }

    # ── single-iteration API (exposed for testing) ──

    def poll_once(self) -> int:
        """Do one getUpdates call and dispatch any returned updates.

        Returns number of updates processed. Raises on transport errors so
        tests can assert. The worker loop wraps this with try/except.
        """
        from datetime import datetime, timezone

        updates = self._transport.get_updates(
            offset=self._offset, timeout_seconds=self._timeout_seconds
        )
        processed = 0
        highest_update_id = self._offset - 1
        for update in updates:
            update_id = self._extract_update_id(update)
            if update_id is None:
                continue
            if update_id > highest_update_id:
                highest_update_id = update_id
            self._router.dispatch(update)
            processed += 1
        if highest_update_id + 1 > self._offset:
            self._offset = highest_update_id + 1
        self._updates_processed += processed
        self._polls_ok += 1
        self._last_successful_poll_at = datetime.now(timezone.utc).isoformat()
        return processed

    # ── internal loop ──

    def _run(self) -> None:
        backoff = _MIN_BACKOFF
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 — loop must not die
                self._polls_failed += 1
                logger.warning(
                    "poller getUpdates failed: %s (retry in %.1fs)", exc, backoff
                )
                # Exponential backoff capped at 30s; stop_event.wait allows
                # fast shutdown during a retry sleep.
                if self._stop_event.wait(backoff):
                    break
                backoff = min(_MAX_BACKOFF, backoff * 2)
                continue
            # Successful iteration resets backoff.
            backoff = _MIN_BACKOFF

    @staticmethod
    def _extract_update_id(update: Mapping[str, Any]) -> Optional[int]:
        raw = update.get("update_id")
        if isinstance(raw, int):
            return raw
        try:
            return int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
