"""SQLite-backed outbox for pending notification sends.

Semantics (L2 persistence — see CLAUDE.md event persistence tiers):
- Writes through a single long-lived connection in WAL mode. Process crashes
  after ``enqueue`` return do NOT lose entries; the dispatcher replays any
  ``pending`` rows on next start.
- Retries are driven by ``next_retry_at``. The transport worker fetches due
  rows with ``fetch_due(now)``; on network failure it bumps the attempt count
  and pushes ``next_retry_at`` forward by the configured backoff.
- After ``max_attempts`` a row transitions to ``dlq``. DLQ entries are kept
  for operator inspection and are reported through the health/status API.

Indexes:
- ``idx_status_retry`` covers the worker's hot path (``status=pending AND
  next_retry_at <= ?``).
- ``event_id`` is UNIQUE — callers can safely retry ``enqueue`` after a
  dispatcher crash mid-submission; duplicates are rejected instead of
  producing double sends.
"""

from __future__ import annotations

import enum
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

from src.utils.sqlite_conn import make_sqlite_conn

logger = logging.getLogger(__name__)


class OutboxStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"  # transient, will retry
    DLQ = "dlq"  # terminal — exceeded max_attempts


_SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    rendered_text TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    next_retry_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_status_retry
    ON notification_outbox(status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_dedup_key
    ON notification_outbox(dedup_key);
"""


def _isoformat(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


def _parse_iso(value: str) -> datetime:
    # datetime.fromisoformat handles +00:00 offset from ``_isoformat`` round-trip.
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class OutboxEntry:
    id: int
    event_id: str
    event_type: str
    severity: str
    chat_id: str
    rendered_text: str
    dedup_key: str
    created_at: datetime
    attempt_count: int
    last_error: str | None
    next_retry_at: datetime
    status: OutboxStatus


class OutboxStore:
    """Thread-safe SQLite store for the notification outbox."""

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = str(path)
        self._conn = make_sqlite_conn(self._db_path)
        self._lock = threading.RLock()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                logger.debug("outbox close ignored error", exc_info=True)

    def __enter__(self) -> "OutboxStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def enqueue(
        self,
        *,
        event_id: str,
        event_type: str,
        severity: str,
        chat_id: str,
        rendered_text: str,
        dedup_key: str,
        now: datetime | None = None,
    ) -> int:
        """Insert a pending row. Returns the row id.

        Raises ``ValueError`` if the ``event_id`` already exists — callers
        should treat this as a duplicate submission and skip.
        """
        created_at = now if now is not None else datetime.now(timezone.utc)
        with self._lock:
            try:
                cursor = self._conn.execute(
                    """
                    INSERT INTO notification_outbox (
                        event_id, event_type, severity, chat_id, rendered_text,
                        dedup_key, created_at, next_retry_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        event_type,
                        severity,
                        chat_id,
                        rendered_text,
                        dedup_key,
                        _isoformat(created_at),
                        _isoformat(created_at),
                        OutboxStatus.PENDING.value,
                    ),
                )
                self._conn.commit()
                row_id = cursor.lastrowid
                assert row_id is not None
                return int(row_id)
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"event_id already in outbox: {event_id}") from exc

    def fetch_due(
        self, *, now: datetime | None = None, limit: int = 10
    ) -> list[OutboxEntry]:
        if limit <= 0:
            return []
        when = now if now is not None else datetime.now(timezone.utc)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, event_id, event_type, severity, chat_id, rendered_text,
                       dedup_key, created_at, attempt_count, last_error,
                       next_retry_at, status
                FROM notification_outbox
                WHERE status = ? AND next_retry_at <= ?
                ORDER BY id
                LIMIT ?
                """,
                (OutboxStatus.PENDING.value, _isoformat(when), limit),
            ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def mark_sent(self, row_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE notification_outbox SET status = ?, last_error = NULL "
                "WHERE id = ?",
                (OutboxStatus.SENT.value, row_id),
            )
            self._conn.commit()

    def mark_failed(
        self,
        row_id: int,
        *,
        error: str,
        next_retry_at: datetime,
    ) -> None:
        """Mark a row failed-but-retryable with a new retry time."""
        with self._lock:
            self._conn.execute(
                """
                UPDATE notification_outbox
                SET status = ?, attempt_count = attempt_count + 1,
                    last_error = ?, next_retry_at = ?
                WHERE id = ?
                """,
                (
                    OutboxStatus.PENDING.value,
                    error,
                    _isoformat(next_retry_at),
                    row_id,
                ),
            )
            self._conn.commit()

    def mark_dlq(self, row_id: int, *, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE notification_outbox SET status = ?, attempt_count = attempt_count + 1, "
                "last_error = ? WHERE id = ?",
                (OutboxStatus.DLQ.value, error, row_id),
            )
            self._conn.commit()

    def count_by_status(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM notification_outbox GROUP BY status"
            ).fetchall()
        return {str(status): int(count) for status, count in rows}

    def dlq_entries(self, limit: int = 50) -> list[OutboxEntry]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, event_id, event_type, severity, chat_id, rendered_text,
                       dedup_key, created_at, attempt_count, last_error,
                       next_retry_at, status
                FROM notification_outbox
                WHERE status = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (OutboxStatus.DLQ.value, limit),
            ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def purge_sent_older_than(self, cutoff: datetime) -> int:
        """Housekeeping: drop ``status=sent`` rows older than ``cutoff``.

        Runs on a timer (default every 6h) in the dispatcher. Returns the
        number of rows deleted.
        """
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM notification_outbox WHERE status = ? AND created_at < ?",
                (OutboxStatus.SENT.value, _isoformat(cutoff)),
            )
            self._conn.commit()
            return cursor.rowcount or 0


def compute_next_retry_at(
    *,
    attempt_count: int,
    backoff_seconds: Sequence[float],
    now: datetime,
) -> datetime:
    """Map ``attempt_count`` (0-indexed) to its next retry timestamp.

    If we have exhausted the backoff list the caller should route to DLQ
    rather than calling this function; we clamp to the last entry for safety.
    """
    if not backoff_seconds:
        raise ValueError("backoff_seconds must be non-empty")
    index = min(attempt_count, len(backoff_seconds) - 1)
    return now + timedelta(seconds=float(backoff_seconds[index]))


def _row_to_entry(row: Iterable) -> OutboxEntry:
    (
        row_id,
        event_id,
        event_type,
        severity,
        chat_id,
        rendered_text,
        dedup_key,
        created_at,
        attempt_count,
        last_error,
        next_retry_at,
        status,
    ) = tuple(row)
    return OutboxEntry(
        id=int(row_id),
        event_id=str(event_id),
        event_type=str(event_type),
        severity=str(severity),
        chat_id=str(chat_id),
        rendered_text=str(rendered_text),
        dedup_key=str(dedup_key),
        created_at=_parse_iso(str(created_at)),
        attempt_count=int(attempt_count),
        last_error=None if last_error is None else str(last_error),
        next_retry_at=_parse_iso(str(next_retry_at)),
        status=OutboxStatus(str(status)),
    )
