from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 每隔多少次 stats 累积后写入 DB（降低写放大）
_STATS_FLUSH_EVERY = 20


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


def _make_conn(db_path: str) -> sqlite3.Connection:
    """创建持久化 SQLite 连接，启用 WAL 和性能 PRAGMA。"""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA cache_size=-8192")  # 8 MB page cache
    return conn


class LocalEventStore:
    """Durable OHLC event store backed by SQLite.

    改造要点（相比原版）：
    - 持久化连接（_get_conn），消除每次操作的 connect/close 开销
    - WAL 模式 + busy_timeout，提升并发读写能力
    - event_stats 延迟写入（_pending_stats），每 _STATS_FLUSH_EVERY 次操作才落盘
    - claim_next_events 使用 UPDATE…RETURNING CTE，一条 SQL 完成原子领取
    - close() 方法：刷新 pending stats 并关闭连接
    """

    def __init__(self, db_path: str = "events.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        # 延迟统计：{date: {field: delta}}
        self._pending_stats: Dict[str, Dict[str, int]] = {}
        self._pending_stats_count = 0
        self._init_db()

    # ─── 连接管理 ───────────────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        """返回持久化连接（首次调用时创建）。调用方需持有 self._lock。"""
        if self._conn is None:
            self._conn = _make_conn(self.db_path)
        return self._conn

    def close(self) -> None:
        """刷新 pending stats 并关闭连接（进程退出 / 测试清理时调用）。"""
        with self._lock:
            if self._conn is not None:
                try:
                    cursor = self._conn.cursor()
                    self._flush_stats(cursor)
                    self._conn.commit()
                except Exception:
                    logger.debug("event_store close: stats flush failed", exc_info=True)
                finally:
                    self._conn.close()
                    self._conn = None

    # ─── 初始化 ─────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with self._lock:
            conn = self._get_conn()
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
                CREATE UNIQUE INDEX IF NOT EXISTS idx_event_identity
                ON ohlc_events(symbol, timeframe, bar_time)
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
        logger.info("LocalEventStore initialized with database: %s", self.db_path)

    @staticmethod
    def _ensure_column(cursor, table: str, column: str, definition: str) -> None:
        cursor.execute(f"PRAGMA table_info({table})")
        existing_columns = {row[1] for row in cursor.fetchall()}
        if column not in existing_columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # ─── 延迟 stats ─────────────────────────────────────────────────────────

    def _accumulate_stats(self, date: str, **kwargs: int) -> None:
        """在内存中累积 stats 增量，不立即写 DB。"""
        bucket = self._pending_stats.setdefault(date, {})
        for k, v in kwargs.items():
            bucket[k] = bucket.get(k, 0) + v
        self._pending_stats_count += 1

    def _flush_stats(self, cursor) -> None:
        """将内存累积的 stats 增量批量写入 DB（调用方需持有锁且已开启事务）。"""
        if not self._pending_stats:
            return
        for date, deltas in self._pending_stats.items():
            if not deltas:
                continue
            fields = list(deltas.keys())
            cursor.execute(
                "INSERT OR IGNORE INTO event_stats (date) VALUES (?)", (date,)
            )
            set_clause = ", ".join(f"{f} = {f} + ?" for f in fields)
            cursor.execute(
                f"UPDATE event_stats SET {set_clause} WHERE date = ?",
                (*[deltas[f] for f in fields], date),
            )
        self._pending_stats.clear()
        self._pending_stats_count = 0

    def _maybe_flush_stats(self, cursor) -> None:
        """达到阈值时自动刷新 stats。"""
        if self._pending_stats_count >= _STATS_FLUSH_EVERY:
            self._flush_stats(cursor)

    # ─── 写入 ────────────────────────────────────────────────────────────────

    def publish_event(self, symbol: str, timeframe: str, bar_time: datetime) -> int:
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            now = _utc_now()
            before_changes = conn.total_changes
            cursor.execute(
                """
                INSERT OR IGNORE INTO ohlc_events (symbol, timeframe, bar_time, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (symbol, timeframe, bar_time.isoformat(), now.isoformat()),
            )
            inserted = conn.total_changes - before_changes
            if inserted:
                today = now.date().isoformat()
                self._accumulate_stats(today, total_events=1)
                event_id = int(cursor.lastrowid)
            else:
                cursor.execute(
                    """
                    SELECT id FROM ohlc_events
                    WHERE symbol = ? AND timeframe = ? AND bar_time = ?
                    """,
                    (symbol, timeframe, bar_time.isoformat()),
                )
                row = cursor.fetchone()
                event_id = int(row[0])
            self._maybe_flush_stats(cursor)
            conn.commit()
            logger.debug(
                "Published event: %s/%s at %s, id=%s",
                symbol,
                timeframe,
                bar_time,
                event_id,
            )
            return event_id

    def publish_events_batch(
        self,
        events: List[tuple[str, str, datetime]],
    ) -> int:
        if not events:
            return 0

        unique_events: list[tuple[str, str, datetime]] = list(
            dict.fromkeys(
                (symbol, timeframe, bar_time)
                for symbol, timeframe, bar_time in events
            )
        )

        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            now = _utc_now()
            before_changes = conn.total_changes
            cursor.executemany(
                """
                INSERT OR IGNORE INTO ohlc_events (symbol, timeframe, bar_time, created_at)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (symbol, timeframe, bar_time.isoformat(), now.isoformat())
                    for symbol, timeframe, bar_time in unique_events
                ],
            )
            inserted = conn.total_changes - before_changes
            if inserted:
                today = now.date().isoformat()
                self._accumulate_stats(today, total_events=inserted)
            self._maybe_flush_stats(cursor)
            conn.commit()
            if inserted:
                logger.debug(
                    "Published %s/%s OHLC events in batch (%s unique, %s inserted)",
                    len(events),
                    len(unique_events),
                    len(unique_events),
                    inserted,
                )
            return inserted

    # ─── 领取（原子 UPDATE…RETURNING） ──────────────────────────────────────

    def claim_next_events(self, limit: int = 1) -> List[ClaimedEvent]:
        limit = max(1, int(limit))
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            now_str = _utc_now().isoformat()
            # 单条 SQL 原子完成：SELECT 未处理 + UPDATE 为 in-flight + RETURNING 结果
            cursor.execute(
                """
                UPDATE ohlc_events
                SET processed = 1, processed_at = ?
                WHERE id IN (
                    SELECT id FROM ohlc_events
                    WHERE processed = 0
                    ORDER BY retry_count ASC, bar_time ASC
                    LIMIT ?
                )
                RETURNING id, symbol, timeframe, bar_time
                """,
                (now_str, limit),
            )
            rows = cursor.fetchall()
            if not rows:
                return []
            conn.commit()
            return [
                ClaimedEvent(
                    event_id=int(r[0]),
                    symbol=str(r[1]),
                    timeframe=str(r[2]),
                    bar_time=_parse_bar_time(r[3]),
                )
                for r in rows
            ]

    def claim_next_event(self) -> Optional[ClaimedEvent]:
        events = self.claim_next_events(limit=1)
        return events[0] if events else None

    # ─── 状态更新 ────────────────────────────────────────────────────────────

    def mark_event_completed_by_id(
        self,
        event_id: int,
        outcome: str = "completed",
        detail: str = "",
    ) -> bool:
        with self._lock:
            conn = self._get_conn()
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
                skipped = 1 if outcome.startswith("skipped_") else 0
                self._accumulate_stats(
                    today,
                    processed_events=1,
                    skipped_events=skipped,
                )
            self._maybe_flush_stats(cursor)
            conn.commit()
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
            conn = self._get_conn()
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
                self._accumulate_stats(today, failed_events=1)
                cursor.execute(
                    "SELECT retry_count FROM ohlc_events WHERE id = ?", (int(event_id),)
                )
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
                    logger.error(
                        "Event %s permanently failed after %s retries",
                        event_id,
                        retry_count,
                    )
            self._maybe_flush_stats(cursor)
            conn.commit()
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

    # ─── 维护 ────────────────────────────────────────────────────────────────

    def cleanup_old_events(self, days_to_keep: int = 7) -> None:
        cutoff = _utc_now() - timedelta(days=days_to_keep)
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            # 清理前先刷新 pending stats，保持一致性
            self._flush_stats(cursor)
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
        if deleted_count > 0:
            logger.info(
                "Cleaned up %s old events (older than %s days)", deleted_count, days_to_keep
            )

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            # 查询前先刷新 stats，确保数据最新
            self._flush_stats(cursor)
            conn.commit()
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
            stats: Dict[str, Any] = {
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
                SELECT symbol,
                       COUNT(*),
                       SUM(CASE WHEN processed = 2 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN outcome LIKE 'skipped_%' THEN 1 ELSE 0 END)
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
                SELECT timeframe,
                       COUNT(*),
                       SUM(CASE WHEN processed = 2 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN outcome LIKE 'skipped_%' THEN 1 ELSE 0 END)
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

            cursor.execute(
                "SELECT COUNT(*) FROM ohlc_events WHERE processed = 0 AND retry_count > 0"
            )
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
        return stats

    def reset_processing_events(self) -> int:
        with self._lock:
            conn = self._get_conn()
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
        if reset_count > 0:
            logger.warning("Reset %s in-flight OHLC events after restart", reset_count)
        return reset_count

    def reset_failed_events(self, max_retries: int = 3) -> int:
        with self._lock:
            conn = self._get_conn()
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
        if reset_count > 0:
            logger.info("Reset %s failed events for retry", reset_count)
        return reset_count


# ─── No-op 实现（回测 / 测试专用）─────────────────────────────────────────────


class NullEventStore:
    """回测 / 测试用的空实现，满足 LocalEventStore 的全部接口但不做任何 I/O。"""

    def __init__(self) -> None:
        self._next_id = 1

    def publish_event(self, symbol: str, timeframe: str, bar_time: datetime) -> int:
        eid = self._next_id
        self._next_id += 1
        return eid

    def publish_events_batch(
        self, events: List[tuple[str, str, datetime]]
    ) -> int:
        n = len(events)
        self._next_id += n
        return n

    def claim_next_events(self, limit: int = 1) -> List[ClaimedEvent]:
        return []

    def claim_next_event(self) -> Optional[ClaimedEvent]:
        return None

    def mark_event_completed_by_id(self, event_id: int, outcome: str = "completed", detail: str = "") -> bool:
        return True

    def mark_event_skipped_by_id(self, event_id: int, reason: str) -> bool:
        return True

    def mark_event_failed_by_id(self, event_id: int, error_msg: str = "") -> bool:
        return True

    def cleanup_old_events(self, days_to_keep: int = 7) -> None:
        pass

    def get_stats(self) -> Dict[str, Any]:
        return {
            "total": 0, "pending": 0, "processing": 0, "completed": 0,
            "skipped": 0, "failed": 0, "retrying": 0, "total_retries": 0,
            "outcome_counts": {}, "by_symbol": {}, "by_timeframe": {},
            "recent_errors": [], "recent_retryable_errors": [], "recent_skips": [],
        }

    def reset_processing_events(self) -> int:
        return 0

    def reset_failed_events(self, max_retries: int = 3) -> int:
        return 0

    def close(self) -> None:
        pass


# ─── 单例 ──────────────────────────────────────────────────────────────────────

_event_store_instances: Dict[str, LocalEventStore] = {}


def get_event_store(db_path: str = "events.db") -> LocalEventStore:
    normalized_path = os.path.abspath(db_path)
    instance = _event_store_instances.get(normalized_path)
    if instance is None:
        instance = LocalEventStore(normalized_path)
        _event_store_instances[normalized_path] = instance
    return instance


def close_event_store(
    *,
    db_path: str | None = None,
    instance: LocalEventStore | None = None,
) -> None:
    keys_to_close: list[str] = []
    if instance is not None:
        keys_to_close.extend(
            key for key, value in _event_store_instances.items() if value is instance
        )
    elif db_path is not None:
        keys_to_close.append(os.path.abspath(db_path))

    for key in keys_to_close:
        store = _event_store_instances.pop(key, None)
        if store is not None:
            store.close()
