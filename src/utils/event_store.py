"""
基于SQLite的本地事件存储，确保OHLC事件不丢失。
用于替代内存队列，提高事件驱动的可靠性。
"""

import sqlite3
import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, List, Dict, Any
import logging

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LocalEventStore:
    """基于SQLite的本地事件存储，确保事件不丢失"""
    
    def __init__(self, db_path: str = "events.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()
    
    def _init_db(self):
        """初始化数据库表结构"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 创建OHLC事件表
            cursor.execute("""
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
            """)
            
            # 创建索引
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_unprocessed 
                ON ohlc_events(processed, symbol, timeframe)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_time 
                ON ohlc_events(bar_time)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_retry 
                ON ohlc_events(retry_count, processed)
            """)
            
            # 创建事件统计表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS event_stats (
                    date TEXT PRIMARY KEY,
                    total_events INTEGER DEFAULT 0,
                    processed_events INTEGER DEFAULT 0,
                    skipped_events INTEGER DEFAULT 0,
                    failed_events INTEGER DEFAULT 0,
                    avg_processing_time_ms REAL DEFAULT 0
                )
            """)

            self._ensure_column(cursor, "ohlc_events", "outcome", "TEXT")
            self._ensure_column(cursor, "event_stats", "skipped_events", "INTEGER DEFAULT 0")
            
            conn.commit()
            conn.close()
        
        logger.info(f"LocalEventStore initialized with database: {self.db_path}")

    @staticmethod
    def _ensure_column(cursor, table: str, column: str, definition: str) -> None:
        cursor.execute(f"PRAGMA table_info({table})")
        existing_columns = {row[1] for row in cursor.fetchall()}
        if column not in existing_columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    
    def publish_event(self, symbol: str, timeframe: str, bar_time: datetime) -> int:
        """
        发布OHLC事件
        
        Args:
            symbol: 交易品种
            timeframe: 时间框架
            bar_time: K线时间
            
        Returns:
            事件ID
        """
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 检查是否已存在相同事件（避免重复）
            cursor.execute("""
                SELECT id FROM ohlc_events 
                WHERE symbol = ? AND timeframe = ? AND bar_time = ?
            """, (symbol, timeframe, bar_time.isoformat()))
            
            existing = cursor.fetchone()
            if existing:
                conn.close()
                return existing[0]
            
            # 插入新事件
            cursor.execute("""
                INSERT INTO ohlc_events (symbol, timeframe, bar_time, created_at)
                VALUES (?, ?, ?, ?)
            """, (
                symbol, 
                timeframe, 
                bar_time.isoformat(), 
                _utc_now().isoformat()
            ))
            
            event_id = cursor.lastrowid
            
            # 更新统计
            today = _utc_now().date().isoformat()
            cursor.execute("""
                INSERT OR IGNORE INTO event_stats (date) VALUES (?)
            """, (today,))
            cursor.execute("""
                UPDATE event_stats 
                SET total_events = total_events + 1 
                WHERE date = ?
            """, (today,))
            
            conn.commit()
            conn.close()
            
            logger.debug(f"Published event: {symbol}/{timeframe} at {bar_time}, id={event_id}")
            return event_id
    
    def get_next_events(self, limit: int = 1) -> List[Tuple[str, str, datetime]]:
        """Fetch up to *limit* pending events in a single SQLite transaction.

        All selected rows are atomically marked as in-progress (processed=1)
        before returning, so the caller owns them for subsequent
        mark_event_completed / mark_event_failed calls.
        """
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

                results: List[Tuple[str, str, datetime]] = []
                ids: List[int] = []
                for event_id, symbol, timeframe, bar_time_str in rows:
                    try:
                        bar_time = datetime.fromisoformat(bar_time_str)
                    except ValueError:
                        bar_time = datetime.strptime(bar_time_str, "%Y-%m-%d %H:%M:%S")
                    results.append((symbol, timeframe, bar_time))
                    ids.append(event_id)

                now_str = _utc_now().isoformat()
                cursor.executemany(
                    "UPDATE ohlc_events SET processed = 1, processed_at = ? WHERE id = ?",
                    [(now_str, eid) for eid in ids],
                )
                conn.commit()
                logger.debug("Retrieved %s event(s) from store", len(results))
                return results
            finally:
                conn.close()

    def get_next_event(self) -> Optional[Tuple[str, str, datetime]]:
        """Fetch a single pending event (convenience wrapper over get_next_events)."""
        events = self.get_next_events(limit=1)
        return events[0] if events else None
    
    def mark_event_completed(
        self,
        symbol: str,
        timeframe: str,
        bar_time: datetime,
        outcome: str = "completed",
        detail: str = "",
    ) -> bool:
        """
        标记事件为已完成
        
        Args:
            symbol: 交易品种
            timeframe: 时间框架
            bar_time: K线时间
            
        Returns:
            是否成功标记
        """
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute("""
                UPDATE ohlc_events 
                SET processed = 2, processed_at = ?, outcome = ?, error_message = ?
                WHERE symbol = ? AND timeframe = ? AND bar_time = ? AND processed = 1
            """, (
                _utc_now().isoformat(),
                outcome,
                detail[:500] if detail else None,
                symbol,
                timeframe,
                bar_time.isoformat()
            ))
            
            updated = cursor.rowcount > 0
            retry_count = 0
            
            if updated:
                # 更新统计
                today = _utc_now().date().isoformat()
                cursor.execute("""
                    UPDATE event_stats 
                    SET processed_events = processed_events + 1,
                        skipped_events = skipped_events + ?
                    WHERE date = ?
                """, (1 if outcome.startswith("skipped_") else 0, today))
            
            conn.commit()
            conn.close()
            
            if updated:
                logger.debug(f"Marked event as completed: {symbol}/{timeframe} at {bar_time}")
            else:
                logger.warning(f"Failed to mark event as completed: {symbol}/{timeframe} at {bar_time}")
            
            return updated

    def mark_event_skipped(self, symbol: str, timeframe: str, bar_time: datetime, reason: str) -> bool:
        return self.mark_event_completed(
            symbol,
            timeframe,
            bar_time,
            outcome=f"skipped_{reason}",
            detail=reason,
        )
    
    def mark_event_failed(self, symbol: str, timeframe: str, bar_time: datetime, error_msg: str = "") -> bool:
        """
        标记事件为失败（将重试）
        
        Args:
            symbol: 交易品种
            timeframe: 时间框架
            bar_time: K线时间
            error_msg: 错误信息
            
        Returns:
            是否成功标记
        """
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 增加重试计数，重置处理状态
            cursor.execute("""
                UPDATE ohlc_events 
                SET processed = 0, 
                    retry_count = retry_count + 1,
                    error_message = ?,
                    outcome = 'failed_transient'
                WHERE symbol = ? AND timeframe = ? AND bar_time = ?
            """, (
                error_msg[:500],  # 限制错误信息长度
                symbol,
                timeframe,
                bar_time.isoformat()
            ))
            
            updated = cursor.rowcount > 0
            
            if updated:
                # 更新统计
                today = _utc_now().date().isoformat()
                cursor.execute("""
                    UPDATE event_stats 
                    SET failed_events = failed_events + 1 
                    WHERE date = ?
                """, (today,))
                
                # 检查是否超过最大重试次数
                cursor.execute("""
                    SELECT retry_count FROM ohlc_events 
                    WHERE symbol = ? AND timeframe = ? AND bar_time = ?
                """, (symbol, timeframe, bar_time.isoformat()))
                
                retry_count = cursor.fetchone()[0]
                if retry_count >= 3:  # 最大重试3次
                    cursor.execute("""
                        UPDATE ohlc_events 
                        SET processed = 3,
                            outcome = 'failed_permanent'
                        WHERE symbol = ? AND timeframe = ? AND bar_time = ?
                    """, (symbol, timeframe, bar_time.isoformat()))
                    logger.error(f"Event permanently failed after {retry_count} retries: {symbol}/{timeframe} at {bar_time}")
            
            conn.commit()
            conn.close()
            
            if updated:
                if retry_count == 1:
                    logger.warning(
                        "Marked event as failed and queued for retry: %s/%s at %s",
                        symbol,
                        timeframe,
                        bar_time,
                    )
                elif retry_count < 3:
                    logger.debug(
                        "Retryable event failure persisted: %s/%s at %s (retry_count=%s)",
                        symbol,
                        timeframe,
                        bar_time,
                        retry_count,
                    )
            
            return updated
    
    def cleanup_old_events(self, days_to_keep: int = 7):
        """
        清理旧事件
        
        Args:
            days_to_keep: 保留天数
        """
        cutoff = _utc_now() - timedelta(days=days_to_keep)
        
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 删除旧事件
            cursor.execute("""
                DELETE FROM ohlc_events 
                WHERE bar_time < ?
            """, (cutoff.isoformat(),))
            
            deleted_count = cursor.rowcount
            
            # 清理旧统计
            cutoff_date = cutoff.date().isoformat()
            cursor.execute("""
                DELETE FROM event_stats 
                WHERE date < ?
            """, (cutoff_date,))
            
            conn.commit()
            conn.close()
        
        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} old events (older than {days_to_keep} days)")
    
    def get_stats(self) -> Dict[str, Any]:
        """
        获取事件统计信息
        
        Returns:
            统计信息字典
        """
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 总体统计
            cursor.execute("""
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN processed = 0 THEN 1 ELSE 0 END) as pending,
                    SUM(CASE WHEN processed = 1 THEN 1 ELSE 0 END) as processing,
                    SUM(CASE WHEN processed = 2 THEN 1 ELSE 0 END) as completed,
                    SUM(CASE WHEN outcome LIKE 'skipped_%' THEN 1 ELSE 0 END) as skipped,
                    SUM(CASE WHEN processed = 3 THEN 1 ELSE 0 END) as failed,
                    SUM(retry_count) as total_retries
                FROM ohlc_events
            """)
            
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

            cursor.execute("""
                SELECT COALESCE(outcome, 'completed') AS outcome, COUNT(*)
                FROM ohlc_events
                GROUP BY COALESCE(outcome, 'completed')
            """)
            for outcome, count in cursor.fetchall():
                stats["outcome_counts"][outcome] = count
            
            # 按品种统计
            cursor.execute("""
                SELECT 
                    symbol,
                    COUNT(*) as total,
                    SUM(CASE WHEN processed = 2 THEN 1 ELSE 0 END) as completed,
                    SUM(CASE WHEN outcome LIKE 'skipped_%' THEN 1 ELSE 0 END) as skipped
                FROM ohlc_events
                GROUP BY symbol
            """)
            
            for symbol, total, completed, skipped in cursor.fetchall():
                computed = completed - skipped
                stats["by_symbol"][symbol] = {
                    "total": total,
                    "completed": computed,
                    "skipped": skipped,
                    "completion_rate": computed / total if total > 0 else 0
                }
            
            # 按时间框架统计
            cursor.execute("""
                SELECT 
                    timeframe,
                    COUNT(*) as total,
                    SUM(CASE WHEN processed = 2 THEN 1 ELSE 0 END) as completed,
                    SUM(CASE WHEN outcome LIKE 'skipped_%' THEN 1 ELSE 0 END) as skipped
                FROM ohlc_events
                GROUP BY timeframe
            """)
            
            for timeframe, total, completed, skipped in cursor.fetchall():
                computed = completed - skipped
                stats["by_timeframe"][timeframe] = {
                    "total": total,
                    "completed": computed,
                    "skipped": skipped,
                    "completion_rate": computed / total if total > 0 else 0
                }

            cursor.execute("""
                SELECT COUNT(*)
                FROM ohlc_events
                WHERE processed = 0 AND retry_count > 0
            """)
            stats["retrying"] = cursor.fetchone()[0] or 0
            
            # 最近错误
            cursor.execute("""
                SELECT symbol, timeframe, bar_time, error_message, retry_count
                FROM ohlc_events
                WHERE processed = 3
                  AND error_message IS NOT NULL
                  AND error_message != ''
                ORDER BY id DESC
                LIMIT 10
            """)
            
            for symbol, timeframe, bar_time, error_msg, retry_count in cursor.fetchall():
                stats["recent_errors"].append({
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "bar_time": bar_time,
                    "error_message": error_msg,
                    "retry_count": retry_count
                })

            cursor.execute("""
                SELECT symbol, timeframe, bar_time, error_message, retry_count
                FROM ohlc_events
                WHERE processed = 0
                  AND retry_count > 0
                  AND error_message IS NOT NULL
                  AND error_message != ''
                ORDER BY id DESC
                LIMIT 10
            """)

            for symbol, timeframe, bar_time, error_msg, retry_count in cursor.fetchall():
                stats["recent_retryable_errors"].append({
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "bar_time": bar_time,
                    "error_message": error_msg,
                    "retry_count": retry_count
                })

            cursor.execute("""
                SELECT symbol, timeframe, bar_time, outcome, error_message
                FROM ohlc_events
                WHERE outcome LIKE 'skipped_%'
                ORDER BY id DESC
                LIMIT 10
            """)

            for symbol, timeframe, bar_time, outcome, detail in cursor.fetchall():
                stats["recent_skips"].append({
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "bar_time": bar_time,
                    "outcome": outcome,
                    "detail": detail,
                })
            
            conn.close()
             
            return stats

    def reset_processing_events(self) -> int:
        """Move in-flight events back to pending on process restart."""
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
        """
        重置失败事件（用于手动恢复）
        
        Args:
            max_retries: 最大重试次数
            
        Returns:
            重置的事件数量
        """
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 重置未超过最大重试次数的失败事件
            cursor.execute("""
                UPDATE ohlc_events 
                SET processed = 0, error_message = NULL
                WHERE processed = 3 AND retry_count < ?
            """, (max_retries,))
            
            reset_count = cursor.rowcount
            
            conn.commit()
            conn.close()
        
        if reset_count > 0:
            logger.info(f"Reset {reset_count} failed events for retry")
        
        return reset_count


# 单例实例
_event_store_instance = None

def get_event_store(db_path: str = "events.db") -> LocalEventStore:
    """
    获取事件存储单例
    
    Args:
        db_path: 数据库路径
        
    Returns:
        LocalEventStore实例
    """
    global _event_store_instance
    if _event_store_instance is None:
        _event_store_instance = LocalEventStore(db_path)
    return _event_store_instance
