"""In-memory ring-buffer metrics store.

Replaces SQLite ``health_metrics`` / ``system_status`` tables with pure
in-memory ``collections.deque`` per metric key.  Only ``alert_history``
stays in SQLite (write frequency is negligible).

Design rationale:
- Health metrics are **transient observability data**, not business data.
- After restart the monitoring loop repopulates within seconds.
- Eliminates ~3 million SQLite writes/day and the 3 GB+ file bloat.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 默认参数 ─────────────────────────────────────────────────────────────────
# 每 key 保留条数：2400 × 5 s ≈ 3.3 h（覆盖 generate_report(hours=3)）
_DEFAULT_RING_SIZE = 2400

# alert_history SQLite cache_size (很小，告警频率极低)
_ALERT_CACHE_SIZE_KB = 2048  # 2 MB


# ── 类型别名 ─────────────────────────────────────────────────────────────────
# (timestamp_iso, metric_value, details_json_or_none, alert_level_or_none)
MetricSample = Tuple[str, float, Optional[str], Optional[str]]


def _make_alert_conn(db_path: str) -> sqlite3.Connection:
    from src.utils.sqlite_conn import make_sqlite_conn
    return make_sqlite_conn(db_path, cache_mb=_ALERT_CACHE_SIZE_KB // 1024 or 2, busy_timeout_ms=10000)


_ALERT_DDL = """
CREATE TABLE IF NOT EXISTS alert_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    component TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    alert_level TEXT NOT NULL,
    metric_value REAL NOT NULL,
    threshold REAL NOT NULL,
    message TEXT,
    resolved_at TEXT,
    resolved_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_time
    ON alert_history(timestamp);
"""


class MetricsStore:
    """Thread-safe in-memory ring-buffer store for health metrics.

    Public interface mirrors the old SQLite-based paths so that
    ``HealthMonitor`` and ``reporting.py`` only need minimal changes.
    """

    def __init__(
        self,
        ring_size: int = _DEFAULT_RING_SIZE,
        alert_db_path: Optional[str] = None,
    ) -> None:
        self._ring_size = ring_size
        self._lock = threading.Lock()

        # per-key ring buffer: key = "component.metric_name"
        self._rings: Dict[str, Deque[MetricSample]] = {}

        # latest system_status snapshot (in-memory, replaces system_status table)
        self._system_status: Optional[Dict[str, Any]] = None

        # ── alert SQLite (optional, lightweight) ──
        self._alert_db_path = alert_db_path
        self._alert_conn: Optional[sqlite3.Connection] = None
        if alert_db_path:
            self._init_alert_db()

    # ── ring buffer operations ───────────────────────────────────────────────

    def append(
        self,
        component: str,
        metric_name: str,
        value: float,
        timestamp_iso: str,
        details_json: Optional[str] = None,
        alert_level: Optional[str] = None,
    ) -> None:
        """Append a metric sample to the ring buffer."""
        key = f"{component}.{metric_name}"
        sample: MetricSample = (timestamp_iso, value, details_json, alert_level)
        with self._lock:
            ring = self._rings.get(key)
            if ring is None:
                ring = deque(maxlen=self._ring_size)
                self._rings[key] = ring
            ring.append(sample)

    def get_recent(
        self,
        component: str,
        metric_name: str,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Return most recent *limit* samples for a given key."""
        key = f"{component}.{metric_name}"
        with self._lock:
            ring = self._rings.get(key)
            if not ring:
                return []
            # deque 尾部是最新的
            samples = list(ring)[-limit:] if limit < len(ring) else list(ring)
        return [
            {
                "timestamp": s[0],
                "value": s[1],
                "details": json.loads(s[2]) if s[2] else None,
                "alert_level": s[3],
            }
            for s in samples
        ]

    def iter_window(
        self,
        hours: int = 1,
        now: Optional[datetime] = None,
    ) -> List[Tuple[str, str, float, str]]:
        """Yield (component, metric_name, value, timestamp) within time window.

        Returns a flat list to keep the interface simple.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=hours)).isoformat()

        result: List[Tuple[str, str, float, str]] = []
        with self._lock:
            for key, ring in self._rings.items():
                dot = key.index(".")
                component = key[:dot]
                metric_name = key[dot + 1 :]
                for sample in ring:
                    if sample[0] >= cutoff:
                        result.append((component, metric_name, sample[1], sample[0]))
        return result

    # ── system status snapshot ───────────────────────────────────────────────

    def set_system_status(self, status: Dict[str, Any]) -> None:
        with self._lock:
            self._system_status = status

    def get_system_status(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._system_status

    # ── alert SQLite ─────────────────────────────────────────────────────────

    def _init_alert_db(self) -> None:
        conn = self._get_alert_conn()
        conn.executescript(_ALERT_DDL)
        conn.commit()
        logger.info("Alert history database initialized: %s", self._alert_db_path)

    def _get_alert_conn(self) -> sqlite3.Connection:
        if self._alert_conn is None:
            assert self._alert_db_path is not None
            self._alert_conn = _make_alert_conn(self._alert_db_path)
        return self._alert_conn

    def write_alert(
        self,
        timestamp: str,
        component: str,
        metric_name: str,
        alert_level: str,
        value: float,
        threshold: float,
        message: str,
    ) -> None:
        """Insert or update an active alert row."""
        if not self._alert_db_path:
            return
        conn = self._get_alert_conn()
        cursor = conn.cursor()
        # 查找未解决的同类告警 → 更新而非重复插入
        cursor.execute(
            """
            SELECT id FROM alert_history
            WHERE component = ? AND metric_name = ? AND alert_level = ? AND resolved_at IS NULL
            ORDER BY timestamp DESC LIMIT 1
            """,
            (component, metric_name, alert_level),
        )
        existing = cursor.fetchone()
        if existing:
            cursor.execute(
                "UPDATE alert_history SET timestamp = ?, metric_value = ? WHERE id = ?",
                (timestamp, value, existing[0]),
            )
        else:
            cursor.execute(
                """
                INSERT INTO alert_history
                (timestamp, component, metric_name, alert_level, metric_value, threshold, message)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (timestamp, component, metric_name, alert_level, value, threshold, message),
            )
        conn.commit()

    def resolve_alert(
        self,
        component: str,
        metric_name: str,
        resolved_by: str = "system",
    ) -> bool:
        if not self._alert_db_path:
            return False
        conn = self._get_alert_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE alert_history
            SET resolved_at = ?, resolved_by = ?
            WHERE component = ? AND metric_name = ? AND resolved_at IS NULL
            """,
            (datetime.now(timezone.utc).isoformat(), resolved_by, component, metric_name),
        )
        updated = cursor.rowcount > 0
        conn.commit()
        return updated

    def load_recent_alerts(self, cutoff_iso: str, limit: int = 20) -> List[Dict[str, Any]]:
        if not self._alert_db_path:
            return []
        conn = self._get_alert_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT timestamp, component, metric_name, alert_level, metric_value, threshold, message
            FROM alert_history
            WHERE timestamp > ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (cutoff_iso, limit),
        )
        return [
            {
                "timestamp": row[0],
                "component": row[1],
                "metric_name": row[2],
                "alert_level": row[3],
                "metric_value": row[4],
                "threshold": row[5],
                "message": row[6],
            }
            for row in cursor.fetchall()
        ]

    def cleanup_alerts(self, days_to_keep: int = 30) -> int:
        if not self._alert_db_path:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_to_keep)).isoformat()
        conn = self._get_alert_conn()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM alert_history WHERE timestamp < ? AND resolved_at IS NOT NULL",
            (cutoff,),
        )
        deleted = cursor.rowcount
        conn.commit()
        if deleted:
            # alert 表数据极少，VACUUM 开销可忽略
            conn.execute("VACUUM")
        return deleted

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        if self._alert_conn is not None:
            try:
                self._alert_conn.close()
            except Exception:
                pass
            self._alert_conn = None
