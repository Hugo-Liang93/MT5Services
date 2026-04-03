"""
Lightweight health monitoring with SQLite-backed metrics and alerts.

改造要点（相比原版）：
- 持久化连接（_get_conn），消除 record_metric 每次 connect/close 开销
- WAL 模式 + busy_timeout，提升并发读写能力
- 写入缓冲区（_write_buffer）+ 后台刷盘线程，每 _FLUSH_INTERVAL 秒批量 INSERT
- 告警写入（_record_alert）仍同步执行，确保告警不丢失
- NullHealthMonitor：回测 / 测试用的空实现，零 I/O
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .checks import (
    check_cache_stats,
    check_data_latency,
    check_economic_calendar,
    check_indicator_freshness,
    check_queue_stats,
)
from .common import is_finite_metric_value
from .reporting import (
    cleanup_old_data,
    generate_report,
    get_recent_metrics,
    get_system_status,
)

logger = logging.getLogger(__name__)

# 后台刷盘间隔（秒）
_FLUSH_INTERVAL = 5.0
# 缓冲区达到此条数时立即触发刷盘（防止内存无限增长）
_FLUSH_BUFFER_MAX = 500


def _make_conn(db_path: str) -> sqlite3.Connection:
    """创建持久化 SQLite 连接，启用 WAL 和性能 PRAGMA。"""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA cache_size=-4096")  # 4 MB page cache
    return conn


# 写缓冲行类型：(timestamp, component, metric_name, metric_value, details_json, alert_level)
_MetricRow = Tuple[str, str, str, float, Optional[str], Optional[str]]


class HealthMonitor:
    """SQLite-backed runtime health monitor."""

    def __init__(self, db_path: str = "health_monitor.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

        # 写入缓冲区（延迟落盘，降低 SQLite 写压力）
        self._write_buffer: List[_MetricRow] = []

        # 后台刷盘控制
        self._stop_flush = threading.Event()
        self._flush_thread: Optional[threading.Thread] = None

        self._init_db()
        self.metrics: Dict[str, List[Dict[str, Any]]] = {}
        self.alerts = {
            "data_latency": {"warning": 10.0, "critical": 30.0},
            "indicator_freshness": {"warning": 60.0, "critical": 300.0},
            "queue_depth": {"warning": 1000, "critical": 5000},
            # 指标计算延迟 P99（毫秒）：单次指标计算批次耗时的第 99 百分位。
            "indicator_compute_p99_ms": {"warning": 500.0, "critical": 2000.0},
            # 缓存命中率保留但仅作信息指标（不再用于告警）。
            "cache_hit_rate": {"warning": 0.0, "critical": 0.0},
        }
        self.active_alerts: Dict[str, Dict[str, Any]] = {}
        # Report cache: avoid repeated SQLite full-table scans from SSE polling
        self._report_cache: Optional[Dict[str, Any]] = None
        self._report_cache_at: float = 0.0
        self._report_cache_ttl: float = 15.0  # seconds

        self._start_flush_thread()
        logger.info("HealthMonitor initialized with database: %s", db_path)

    # ─── 连接管理 ───────────────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        """返回持久化连接（首次调用时创建）。调用方需持有 self._lock。"""
        if self._conn is None:
            self._conn = _make_conn(self.db_path)
        return self._conn

    def close(self) -> None:
        """刷新缓冲区并关闭连接（进程退出 / 测试清理时调用）。"""
        self._stop_flush.set()
        if self._flush_thread is not None:
            self._flush_thread.join(timeout=10.0)
        self._flush_buffer()
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    logger.debug("HealthMonitor close: conn.close() failed", exc_info=True)
                finally:
                    self._conn = None

    # ─── 初始化 ─────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS health_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    component TEXT NOT NULL,
                    metric_name TEXT NOT NULL,
                    metric_value REAL NOT NULL,
                    details TEXT,
                    alert_level TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_component_time
                ON health_metrics(component, timestamp)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_metric_time
                ON health_metrics(metric_name, timestamp)
                """
            )
            cursor.execute(
                """
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
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS system_status (
                    timestamp TEXT PRIMARY KEY,
                    overall_status TEXT NOT NULL,
                    components_status TEXT NOT NULL,
                    metrics_summary TEXT NOT NULL
                )
                """
            )
            conn.commit()

    # ─── 后台刷盘 ────────────────────────────────────────────────────────────

    def _start_flush_thread(self) -> None:
        self._flush_thread = threading.Thread(
            target=self._flush_worker,
            daemon=True,
            name="health-monitor-flush",
        )
        self._flush_thread.start()

    def _flush_worker(self) -> None:
        while not self._stop_flush.wait(_FLUSH_INTERVAL):
            self._flush_buffer()

    def _flush_buffer(self) -> None:
        """将缓冲区中的指标行批量写入 DB。"""
        with self._lock:
            if not self._write_buffer:
                return
            rows = self._write_buffer
            self._write_buffer = []
            conn = self._get_conn()
            try:
                conn.executemany(
                    """
                    INSERT INTO health_metrics
                    (timestamp, component, metric_name, metric_value, details, alert_level)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                conn.commit()
            except Exception:
                logger.warning(
                    "HealthMonitor: failed to flush %s metric rows", len(rows), exc_info=True
                )
                # 失败时把数据放回缓冲区（不重复放超限的部分）
                overflow = max(0, len(self._write_buffer) + len(rows) - _FLUSH_BUFFER_MAX)
                self._write_buffer = rows[overflow:] + self._write_buffer

    # ─── 配置 ────────────────────────────────────────────────────────────────

    def configure_alerts(
        self,
        *,
        data_latency_warning: Optional[float] = None,
        data_latency_critical: Optional[float] = None,
    ) -> None:
        if data_latency_warning is not None:
            self.alerts["data_latency"]["warning"] = float(data_latency_warning)
        if data_latency_critical is not None:
            self.alerts["data_latency"]["critical"] = float(data_latency_critical)

    # ─── 核心写入 ────────────────────────────────────────────────────────────

    def record_metric(
        self,
        component: str,
        metric_name: str,
        value: float,
        details: Dict[str, Any] | None = None,
        check_alert: bool = True,
    ) -> None:
        timestamp = self._utc_now().isoformat()
        if not is_finite_metric_value(value):
            logger.debug("Skipping non-finite metric: %s.%s=%r", component, metric_name, value)
            return
        metric_value = float(value)
        alert_level = (
            self._check_alert_level(component, metric_name, metric_value)
            if check_alert
            else None
        )

        # 内存缓存（最后 100 条，用于 get_recent_metrics 快速访问）
        key = f"{component}.{metric_name}"
        self.metrics.setdefault(key, []).append(
            {
                "timestamp": timestamp,
                "value": metric_value,
                "details": details,
                "alert_level": alert_level,
            }
        )
        if len(self.metrics[key]) > 100:
            self.metrics[key] = self.metrics[key][-100:]

        details_json = json.dumps(details) if details else None

        with self._lock:
            # 指标写入缓冲区（延迟落盘）
            self._write_buffer.append(
                (timestamp, component, metric_name, metric_value, details_json, alert_level)
            )
            # 缓冲区超限时立即刷盘（防止内存无限增长）
            if len(self._write_buffer) >= _FLUSH_BUFFER_MAX:
                rows = self._write_buffer
                self._write_buffer = []
                conn = self._get_conn()
                try:
                    conn.executemany(
                        """
                        INSERT INTO health_metrics
                        (timestamp, component, metric_name, metric_value, details, alert_level)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                    conn.commit()
                except Exception:
                    logger.warning(
                        "HealthMonitor: overflow flush failed (%s rows)", len(rows), exc_info=True
                    )

            # 告警写入同步执行（不缓冲，确保告警不丢失）
            if alert_level in {"warning", "critical"}:
                conn = self._get_conn()
                cursor = conn.cursor()
                self._record_alert(
                    cursor,
                    timestamp,
                    component,
                    metric_name,
                    alert_level,
                    metric_value,
                    details,
                )
                conn.commit()

    # ─── 内部辅助 ────────────────────────────────────────────────────────────

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _check_alert_level(
        self,
        component: str,
        metric_name: str,
        value: float,
    ) -> Optional[str]:
        del component
        if metric_name not in self.alerts:
            return None
        thresholds = self.alerts[metric_name]
        if metric_name in {
            "data_latency",
            "indicator_freshness",
            "economic_calendar_staleness",
            "economic_provider_failures",
            "queue_depth",
        }:
            if value >= thresholds["critical"]:
                return "critical"
            if value >= thresholds["warning"]:
                return "warning"
        elif metric_name == "indicator_compute_p99_ms":
            if value >= thresholds["critical"] > 0:
                return "critical"
            if value >= thresholds["warning"] > 0:
                return "warning"
        elif metric_name == "cache_hit_rate":
            pass
        return None

    def _record_alert(
        self,
        cursor,
        timestamp: str,
        component: str,
        metric_name: str,
        alert_level: str,
        value: float,
        details: Dict[str, Any] | None = None,
    ) -> None:
        del details
        alert_key = f"{component}.{metric_name}"
        threshold = self.alerts[metric_name][alert_level]
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
                """
                UPDATE alert_history
                SET timestamp = ?, metric_value = ?
                WHERE id = ?
                """,
                (timestamp, value, existing[0]),
            )
            return

        message = self._generate_alert_message(
            component,
            metric_name,
            alert_level,
            value,
            threshold,
        )
        cursor.execute(
            """
            INSERT INTO alert_history
            (timestamp, component, metric_name, alert_level, metric_value, threshold, message)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (timestamp, component, metric_name, alert_level, value, threshold, message),
        )
        self.active_alerts[alert_key] = {
            "timestamp": timestamp,
            "component": component,
            "metric_name": metric_name,
            "alert_level": alert_level,
            "value": value,
            "threshold": threshold,
            "message": message,
        }
        logger.warning("New alert: %s", message)

    def _generate_alert_message(
        self,
        component: str,
        metric_name: str,
        alert_level: str,
        value: float,
        threshold: float,
    ) -> str:
        if metric_name == "data_latency":
            return (
                f"{component}: data latency {alert_level} - "
                f"current={value:.1f}s threshold={threshold}s"
            )
        if metric_name == "indicator_freshness":
            return (
                f"{component}: indicator freshness {alert_level} - "
                f"current={value:.1f}s threshold={threshold}s"
            )
        if metric_name == "queue_depth":
            return (
                f"{component}: queue depth {alert_level} - "
                f"current={value} threshold={threshold}"
            )
        if metric_name == "indicator_compute_p99_ms":
            return (
                f"{component}: compute P99 {alert_level} - "
                f"current={value:.0f}ms threshold={threshold:.0f}ms"
            )
        if metric_name == "cache_hit_rate":
            return (
                f"{component}: cache hit rate {alert_level} - "
                f"current={value:.1%} threshold={threshold:.1%}"
            )
        return (
            f"{component}.{metric_name}: {alert_level} - "
            f"current={value} threshold={threshold}"
        )

    def resolve_alert(
        self,
        component: str,
        metric_name: str,
        resolved_by: str = "system",
    ) -> bool:
        alert_key = f"{component}.{metric_name}"
        if alert_key not in self.active_alerts:
            return False
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE alert_history
                SET resolved_at = ?, resolved_by = ?
                WHERE component = ? AND metric_name = ? AND resolved_at IS NULL
                """,
                (self._utc_now().isoformat(), resolved_by, component, metric_name),
            )
            conn.commit()
        del self.active_alerts[alert_key]
        logger.info("Resolved alert: %s.%s", component, metric_name)
        return True

    # ─── 代理检查方法（委托给 health_checks 模块） ─────────────────────────

    def check_data_latency(self, component: str, service, symbol: str, timeframe: str) -> float:
        return check_data_latency(self, component, service, symbol, timeframe)

    def check_indicator_freshness(
        self,
        component: str,
        worker,
        symbol: str,
        timeframe: str,
    ) -> float:
        return check_indicator_freshness(self, component, worker, symbol, timeframe)

    def check_queue_stats(self, component: str, ingestor) -> Dict[str, Any]:
        return check_queue_stats(self, component, ingestor)

    def check_cache_stats(self, component: str, worker) -> Dict[str, Any]:
        return check_cache_stats(self, component, worker)

    def check_economic_calendar(self, component: str, service) -> Dict[str, Any]:
        return check_economic_calendar(self, component, service)

    # ─── 报告 ────────────────────────────────────────────────────────────────

    def generate_report(self, hours: int = 24) -> Dict[str, Any]:
        now = time.monotonic()
        if (
            hours == 24
            and self._report_cache is not None
            and now - self._report_cache_at < self._report_cache_ttl
        ):
            return self._report_cache
        report = generate_report(self, hours=hours)
        if hours == 24:
            self._report_cache = report
            self._report_cache_at = now
        return report

    def get_recent_metrics(
        self,
        component: str,
        metric_name: str,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        return get_recent_metrics(self, component, metric_name, limit=limit)

    def cleanup_old_data(self, days_to_keep: int = 30) -> None:
        cleanup_old_data(self, days_to_keep=days_to_keep)

    def get_system_status(self) -> Dict[str, Any]:
        return get_system_status(self)


# ─── No-op 实现（回测 / 测试专用）─────────────────────────────────────────────


class NullHealthMonitor:
    """回测 / 测试用的空实现，满足 HealthMonitor 的全部接口但不做任何 I/O。"""

    def __init__(self) -> None:
        self.metrics: Dict[str, List[Dict[str, Any]]] = {}
        self.alerts: Dict[str, Any] = {}
        self.active_alerts: Dict[str, Any] = {}

    def configure_alerts(self, **kwargs: Any) -> None:
        pass

    def record_metric(
        self,
        component: str,
        metric_name: str,
        value: float,
        details: Dict[str, Any] | None = None,
        check_alert: bool = True,
    ) -> None:
        pass

    def resolve_alert(self, component: str, metric_name: str, resolved_by: str = "system") -> bool:
        return False

    def check_data_latency(self, *args: Any, **kwargs: Any) -> float:
        return 0.0

    def check_indicator_freshness(self, *args: Any, **kwargs: Any) -> float:
        return 0.0

    def check_queue_stats(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {}

    def check_cache_stats(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {}

    def check_economic_calendar(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {}

    def generate_report(self, hours: int = 24) -> Dict[str, Any]:
        return {
            "overall_status": "unknown",
            "components": {},
            "summary": {
                "total_metrics": 0,
                "warning_count": 0,
                "critical_count": 0,
                "advisory_warning_count": 0,
                "advisory_critical_count": 0,
            },
            "active_alerts": [],
            "recent_alerts": [],
        }

    def get_recent_metrics(
        self, component: str, metric_name: str, limit: int = 100
    ) -> List[Dict[str, Any]]:
        return []

    def cleanup_old_data(self, days_to_keep: int = 30) -> None:
        pass

    def get_system_status(self) -> Dict[str, Any]:
        return {
            "overall_status": "unknown",
            "components_status": {},
            "metrics_summary": {},
            "active_alerts": [],
        }

    def close(self) -> None:
        pass


# ─── 单例 ──────────────────────────────────────────────────────────────────────

_health_monitor_instances: Dict[str, HealthMonitor] = {}


def get_health_monitor(db_path: str = "health_monitor.db") -> HealthMonitor:
    normalized_path = os.path.abspath(db_path)
    instance = _health_monitor_instances.get(normalized_path)
    if instance is None:
        instance = HealthMonitor(normalized_path)
        _health_monitor_instances[normalized_path] = instance
    return instance


def close_health_monitor(
    *,
    db_path: str | None = None,
    instance: HealthMonitor | None = None,
) -> None:
    keys_to_close: list[str] = []
    if instance is not None:
        keys_to_close.extend(
            key for key, value in _health_monitor_instances.items() if value is instance
        )
    elif db_path is not None:
        keys_to_close.append(os.path.abspath(db_path))

    for key in keys_to_close:
        monitor = _health_monitor_instances.pop(key, None)
        if monitor is not None:
            monitor.close()
