"""WAL-backed persistent queue for confirmed signal events.

Replaces Python's in-memory ``queue.Queue`` for confirmed events so that
bar-close signals survive process restarts. Uses SQLite WAL mode for
concurrent read/write without blocking.

Intrabar events remain in-memory (best-effort, droppable by design).
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS signal_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    indicators_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    processed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sq_unprocessed
    ON signal_queue(processed, id);
"""

_INSERT_SQL = """
INSERT INTO signal_queue (scope, symbol, timeframe, indicators_json, metadata_json, created_at, processed)
VALUES (?, ?, ?, ?, ?, ?, 0)
"""

_CLAIM_SQL = """
UPDATE signal_queue
SET processed = 1
WHERE id = (
    SELECT id FROM signal_queue
    WHERE processed = 0
    ORDER BY id ASC
    LIMIT 1
)
RETURNING id, scope, symbol, timeframe, indicators_json, metadata_json
"""

_COMPLETE_SQL = "DELETE FROM signal_queue WHERE id = ?"

_RESET_INFLIGHT_SQL = "UPDATE signal_queue SET processed = 0 WHERE processed = 1"

_COUNT_SQL = "SELECT COUNT(*) FROM signal_queue WHERE processed = 0"

_CLEANUP_SQL = "DELETE FROM signal_queue WHERE processed = 1 AND id < ?"


class WalSignalQueue:
    """SQLite WAL-backed queue that mimics ``queue.Queue`` interface.

    Thread-safe: uses a single persistent connection protected by a lock.
    On startup, any in-flight (processed=1) events are reset to pending.

    Interface contract (compatible with SignalRuntime._enqueue / dequeue_event):
    - ``put_nowait(item)`` — non-blocking insert
    - ``put(item, timeout)`` — same (SQLite write is near-instant)
    - ``get_nowait()`` → item or raises ``queue.Empty``
    - ``get(timeout)`` → item or raises ``queue.Empty`` after timeout
    - ``qsize()`` → int
    - ``empty()`` → bool
    - ``maxsize`` attribute (for compatibility, always returns 0 = unbounded)
    """

    maxsize: int = 0  # Unbounded (disk-backed)

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._closed = False
        self._init_db()

    def _init_db(self) -> None:
        conn = self._get_conn()
        with self._lock:
            conn.executescript(_DDL)
            # Reset any in-flight events from previous crash
            cursor = conn.execute(_RESET_INFLIGHT_SQL)
            reset_count = cursor.rowcount
            conn.commit()
            if reset_count > 0:
                logger.info(
                    "WalSignalQueue: reset %d in-flight events to pending",
                    reset_count,
                )
            pending = conn.execute(_COUNT_SQL).fetchone()[0]
            if pending > 0:
                logger.info(
                    "WalSignalQueue: %d pending events recovered from previous session",
                    pending,
                )

    def reopen(self) -> None:
        """Reopen after close(): create fresh connection and reset in-flight events."""
        with self._lock:
            self._closed = False
            self._conn = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            if self._closed:
                raise RuntimeError(
                    "WalSignalQueue is closed; call reopen() before further use"
                )
            from src.utils.sqlite_conn import make_sqlite_conn
            self._conn = make_sqlite_conn(self._db_path, cache_mb=4, busy_timeout_ms=5000)
        return self._conn

    def put_nowait(
        self,
        item: tuple[str, str, str, dict[str, dict[str, float]], dict[str, Any]],
    ) -> None:
        """Insert event into persistent queue. Never raises Full."""
        scope, symbol, timeframe, indicators, metadata = item
        indicators_json = json.dumps(indicators, ensure_ascii=False)
        metadata_json = json.dumps(metadata, ensure_ascii=False, default=str)
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                _INSERT_SQL,
                (scope, symbol, timeframe, indicators_json, metadata_json, time.time()),
            )
            conn.commit()

    def put(
        self,
        item: tuple[str, str, str, dict[str, dict[str, float]], dict[str, Any]],
        timeout: float = 0,
    ) -> None:
        """Same as put_nowait (SQLite write is near-instant)."""
        self.put_nowait(item)

    def get_nowait(
        self,
    ) -> tuple[str, str, str, dict[str, dict[str, float]], dict[str, Any]]:
        """Claim next pending event. Raises ``queue.Empty`` if none."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(_CLAIM_SQL).fetchone()
            if row is None:
                raise queue.Empty
            conn.commit()

        row_id, scope, symbol, timeframe, indicators_json, metadata_json = row
        indicators = json.loads(indicators_json)
        metadata = json.loads(metadata_json)
        # Inject monotonic enqueue time for staleness detection (compatible)
        metadata["_enqueued_at"] = time.monotonic()

        # Delete claimed row (it's now in-memory for processing)
        with self._lock:
            conn = self._get_conn()
            conn.execute(_COMPLETE_SQL, (row_id,))
            conn.commit()

        return (scope, symbol, timeframe, indicators, metadata)

    def get(
        self,
        timeout: float = 0,
    ) -> tuple[str, str, str, dict[str, dict[str, float]], dict[str, Any]]:
        """Blocking get with timeout. Polls SQLite at ~100ms intervals."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self.get_nowait()
            except queue.Empty:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(min(0.1, max(deadline - time.monotonic(), 0)))

    def qsize(self) -> int:
        with self._lock:
            conn = self._get_conn()
            return conn.execute(_COUNT_SQL).fetchone()[0]

    def empty(self) -> bool:
        return self.qsize() == 0

    def close(self) -> None:
        """Close the SQLite connection."""
        with self._lock:
            self._closed = True
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
