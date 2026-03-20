from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import sqlite3
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_bar_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class ClaimedEvent:
    event_id: int
    symbol: str
    timeframe: str
    bar_time: datetime


class LocalEventStore:
    """Durable OHLC event store backed by SQLite."""

    def __init__(self, db_path: str = "events.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS ohlc_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    bar_time TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    processed INTEGER DEFAULT 0,
                    processed_at TEXT,
                    retry_count INTEGER DEFAULT 0,
                    error_message TEXT,
                    outcome TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_unprocessed
                ON ohlc_events(processed, symbol, timeframe)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_time
                ON ohlc_events(bar_time)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_retry
                ON ohlc_events(retry_count, processed)
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS event_stats (
                    date TEXT PRIMARY KEY,
                    total_events INTEGER DEFAULT 0,
                    processed_events INTEGER DEFAULT 0,
                    skipped_events INTEGER DEFAULT 0,
                    failed_events INTEGER DEFAULT 0,
                    avg_processing_time_ms REAL DEFAULT 0
                )
                """
            )
            self._ensure_column(cursor, "ohlc_events", "outcome", "TEXT")
            self._ensure_column(cursor, "event_stats", "skipped_events", "INTEGER DEFAULT 0")
            conn.commit()
            conn.close()
        logger.info("LocalEventStore initialized with database: %s", self.db_path)

    @staticmethod
    def _ensure_column(cursor, table: str, column: str, definition: str) -> None:
        cursor.execute(f"PRAGMA table_info({table})")
        existing_columns = {row[1] for row in cursor.fetchall()}
        if column not in existing_columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def publish_event(self, symbol: str, timeframe: str, bar_time: datetime) -> int:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id FROM ohlc_events
                WHERE symbol = ? AND timeframe = ? AND bar_time = ?
                """,
                (symbol, timeframe, bar_time.isoformat()),
            )
            existing = cursor.fetchone()
            if existing:
                conn.close()
                return int(existing[0])

            cursor.execute(
                """
                INSERT INTO ohlc_events (symbol, timeframe, bar_time, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (symbol, timeframe, bar_time.isoformat(), _utc_now().isoformat()),
            )
            event_id = int(cursor.lastrowid)
            today = _utc_now().date().isoformat()
            cursor.execute("INSERT OR IGNORE INTO event_stats (date) VALUES (?)", (today,))
            cursor.execute(
                """
                UPDATE event_stats
                SET total_events = total_events + 1
                WHERE date = ?
                """,
                (today,),
            )
            conn.commit()
            conn.close()
            logger.debug("Published event: %s/%s at %s, id=%s", symbol, timeframe, bar_time, event_id)
            return event_id

    def claim_next_events(self, limit: int = 1) -> List[ClaimedEvent]:
        limit = max(1, int(limit))
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, symbol, timeframe, bar_time
                    FROM ohlc_events
                    WHERE processed = 0
                    ORDER BY retry_count ASC, bar_time ASC
                    LIMIT ?
                    """,
                    (limit,),
                )
                rows = cursor.fetchall()
                if not rows:
                    return []

                claimed = [
                    ClaimedEvent(
                        event_id=int(event_id),
                        symbol=str(symbol),
                        timeframe=str(timeframe),
                        bar_time=_parse_bar_time(bar_time),
                    )
                    for event_id, symbol, timeframe, bar_time in rows
                ]
                now_str = _utc_now().isoformat()
                cursor.executemany(
                    "UPDATE ohlc_events SET processed = 1, processed_at = ? WHERE id = ?",
                    [(now_str, event.event_id) for event in claimed],
                )
                conn.commit()
                return claimed
            finally:
                conn.close()

    def claim_next_event(self) -> Optional[ClaimedEvent]:
        events = self.claim_next_events(limit=1)
        return events[0] if events else None

    def mark_event_completed_by_id(
        self,
        event_id: int,
        outcome: str = "completed",
        detail: str = "",
    ) -> bool:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE ohlc_events
                SET processed = 2, processed_at = ?, outcome = ?, error_message = ?
                WHERE id = ? AND processed = 1
                """,
                (
                    _utc_now().isoformat(),
                    outcome,
                    detail[:500] if detail else None,
                    int(event_id),
                ),
            )
            updated = cursor.rowcount > 0
            if updated:
                today = _utc_now().date().isoformat()
                cursor.execute(
                    """
                    UPDATE event_stats
                    SET processed_events = processed_events + 1,
                        skipped_events = skipped_events + ?
                    WHERE date = ?
                    """,
                    (1 if outcome.startswith("skipped_") else 0, today),
                )
            conn.commit()
            conn.close()
        if updated:
            logger.debug("Marked event %s as completed", event_id)
        else:
            logger.warning("Failed to mark event %s as completed", event_id)
        return updated

    def mark_event_skipped_by_id(self, event_id: int, reason: str) -> bool:
        return self.mark_event_completed_by_id(
            event_id,
            outcome=f"skipped_{reason}",
            detail=reason,
        )

    def mark_event_failed_by_id(self, event_id: int, error_msg: str = "") -> bool:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE ohlc_events
                SET processed = 0,
                    retry_count = retry_count + 1,
                    error_message = ?,
                    outcome = 'failed_transient'
                WHERE id = ?
                """,
                (error_msg[:500], int(event_id)),
            )
            updated = cursor.rowcount > 0
            retry_count = 0
            if updated:
                today = _utc_now().date().isoformat()
                cursor.execute(
                    """
                    UPDATE event_stats
                    SET failed_events = failed_events + 1
                    WHERE date = ?
                    """,
                    (today,),
                )
                cursor.execute("SELECT retry_count FROM ohlc_events WHERE id = ?", (int(event_id),))
                row = cursor.fetchone()
                retry_count = int(row[0]) if row else 0
                if retry_count >= 3:
                    cursor.execute(
                        """
                        UPDATE ohlc_events
                        SET processed = 3,
                            outcome = 'failed_permanent'
                        WHERE id = ?
                        """,
                        (int(event_id),),
                    )
                    logger.error("Event %s permanently failed after %s retries", event_id, retry_count)
            conn.commit()
            conn.close()
        if updated:
            if retry_count == 1:
                logger.warning("Marked event %s as failed and queued for retry", event_id)
            elif retry_count < 3:
                logger.debug(
                    "Retryable event failure persisted for event %s (retry_count=%s)",
                    event_id,
                    retry_count,
                )
        return updated

    def cleanup_old_events(self, days_to_keep: int = 7) -> None:
        cutoff = _utc_now() - timedelta(days=days_to_keep)
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                DELETE FROM ohlc_events
                WHERE bar_time < ?
                  AND processed IN (2, 3)
                """,
                (cutoff.isoformat(),),
            )
            deleted_count = cursor.rowcount
            cutoff_date = cutoff.date().isoformat()
            cursor.execute("DELETE FROM event_stats WHERE date < ?", (cutoff_date,))
            conn.commit()
            conn.close()
        if deleted_count > 0:
            logger.info("Cleaned up %s old events (older than %s days)", deleted_count, days_to_keep)

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN processed = 0 THEN 1 ELSE 0 END) as pending,
                    SUM(CASE WHEN processed = 1 THEN 1 ELSE 0 END) as processing,
                    SUM(CASE WHEN processed = 2 THEN 1 ELSE 0 END) as completed,
                    SUM(CASE WHEN outcome LIKE 'skipped_%' THEN 1 ELSE 0 END) as skipped,
                    SUM(CASE WHEN processed = 3 THEN 1 ELSE 0 END) as failed,
                    SUM(retry_count) as total_retries
                FROM ohlc_events
                """
            )
            row = cursor.fetchone()
            stats = {
                "total": row[0] or 0,
                "pending": row[1] or 0,
                "processing": row[2] or 0,
                "completed": (row[3] or 0) - (row[4] or 0),
                "skipped": row[4] or 0,
                "failed": row[5] or 0,
                "retrying": 0,
                "total_retries": row[6] or 0,
                "outcome_counts": {},
                "by_symbol": {},
                "by_timeframe": {},
                "recent_errors": [],
                "recent_retryable_errors": [],
                "recent_skips": [],
            }

            cursor.execute(
                """
                SELECT COALESCE(outcome, 'completed') AS outcome, COUNT(*)
                FROM ohlc_events
                GROUP BY COALESCE(outcome, 'completed')
                """
            )
            for outcome, count in cursor.fetchall():
                stats["outcome_counts"][outcome] = count

            cursor.execute(
                """
                SELECT symbol, COUNT(*), SUM(CASE WHEN processed = 2 THEN 1 ELSE 0 END), SUM(CASE WHEN outcome LIKE 'skipped_%' THEN 1 ELSE 0 END)
                FROM ohlc_events
                GROUP BY symbol
                """
            )
            for symbol, total, completed, skipped in cursor.fetchall():
                computed = (completed or 0) - (skipped or 0)
                stats["by_symbol"][symbol] = {
                    "total": total,
                    "completed": computed,
                    "skipped": skipped or 0,
                    "completion_rate": computed / total if total > 0 else 0,
                }

            cursor.execute(
                """
                SELECT timeframe, COUNT(*), SUM(CASE WHEN processed = 2 THEN 1 ELSE 0 END), SUM(CASE WHEN outcome LIKE 'skipped_%' THEN 1 ELSE 0 END)
                FROM ohlc_events
                GROUP BY timeframe
                """
            )
            for timeframe, total, completed, skipped in cursor.fetchall():
                computed = (completed or 0) - (skipped or 0)
                stats["by_timeframe"][timeframe] = {
                    "total": total,
                    "completed": computed,
                    "skipped": skipped or 0,
                    "completion_rate": computed / total if total > 0 else 0,
                }

            cursor.execute("SELECT COUNT(*) FROM ohlc_events WHERE processed = 0 AND retry_count > 0")
            stats["retrying"] = cursor.fetchone()[0] or 0

            cursor.execute(
                """
                SELECT symbol, timeframe, bar_time, error_message, retry_count
                FROM ohlc_events
                WHERE processed = 3
                  AND error_message IS NOT NULL
                  AND error_message != ''
                ORDER BY id DESC
                LIMIT 10
                """
            )
            for symbol, timeframe, bar_time, error_msg, retry_count in cursor.fetchall():
                stats["recent_errors"].append(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "bar_time": bar_time,
                        "error_message": error_msg,
                        "retry_count": retry_count,
                    }
                )

            cursor.execute(
                """
                SELECT symbol, timeframe, bar_time, error_message, retry_count
                FROM ohlc_events
                WHERE processed = 0
                  AND retry_count > 0
                  AND error_message IS NOT NULL
                  AND error_message != ''
                ORDER BY id DESC
                LIMIT 10
                """
            )
            for symbol, timeframe, bar_time, error_msg, retry_count in cursor.fetchall():
                stats["recent_retryable_errors"].append(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "bar_time": bar_time,
                        "error_message": error_msg,
                        "retry_count": retry_count,
                    }
                )

            cursor.execute(
                """
                SELECT symbol, timeframe, bar_time, outcome, error_message
                FROM ohlc_events
                WHERE outcome LIKE 'skipped_%'
                ORDER BY id DESC
                LIMIT 10
                """
            )
            for symbol, timeframe, bar_time, outcome, detail in cursor.fetchall():
                stats["recent_skips"].append(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "bar_time": bar_time,
                        "outcome": outcome,
                        "detail": detail,
                    }
                )
            conn.close()
        return stats

    def reset_processing_events(self) -> int:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE ohlc_events
                SET processed = 0,
                    processed_at = NULL
                WHERE processed = 1
                """
            )
            reset_count = cursor.rowcount
            conn.commit()
            conn.close()
        if reset_count > 0:
            logger.warning("Reset %s in-flight OHLC events after restart", reset_count)
        return reset_count

    def reset_failed_events(self, max_retries: int = 3) -> int:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE ohlc_events
                SET processed = 0, error_message = NULL
                WHERE processed = 3 AND retry_count < ?
                """,
                (max_retries,),
            )
            reset_count = cursor.rowcount
            conn.commit()
            conn.close()
        if reset_count > 0:
            logger.info("Reset %s failed events for retry", reset_count)
        return reset_count


_event_store_instance: Optional[LocalEventStore] = None


def get_event_store(db_path: str = "events.db") -> LocalEventStore:
    global _event_store_instance
    if _event_store_instance is None:
        _event_store_instance = LocalEventStore(db_path)
    return _event_store_instance
