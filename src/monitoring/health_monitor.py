"""
Lightweight health monitoring with SQLite-backed metrics and alerts.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .health_checks import (
    check_cache_stats,
    check_data_latency,
    check_economic_calendar,
    check_indicator_freshness,
    check_queue_stats,
)
from .health_common import is_finite_metric_value
from .health_reporting import (
    cleanup_old_data,
    generate_report,
    generate_report_legacy,
    get_recent_metrics,
    get_system_status,
)

logger = logging.getLogger(__name__)


class HealthMonitor:
    """SQLite-backed runtime health monitor."""

    def __init__(self, db_path: str = "health_monitor.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()
        self.metrics: Dict[str, List[Dict[str, Any]]] = {}
        self.alerts = {
            "data_latency": {"warning": 10.0, "critical": 30.0},
            "indicator_freshness": {"warning": 60.0, "critical": 300.0},
            "queue_depth": {"warning": 1000, "critical": 5000},
            # 指标计算延迟 P99（毫秒）：单次指标计算批次耗时的第 99 百分位。
            # warning=500ms 表示批量计算偶尔超时；critical=2000ms 表示严重性能退化。
            "indicator_compute_p99_ms": {"warning": 500.0, "critical": 2000.0},
            # 缓存命中率保留但仅作信息指标（不再用于告警）。
            "cache_hit_rate": {"warning": 0.0, "critical": 0.0},
        }
        self.active_alerts: Dict[str, Dict[str, Any]] = {}
        logger.info("HealthMonitor initialized with database: %s", db_path)

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

    def _init_db(self) -> None:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
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
            conn.close()

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

        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO health_metrics
                (timestamp, component, metric_name, metric_value, details, alert_level)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    component,
                    metric_name,
                    metric_value,
                    json.dumps(details) if details else None,
                    alert_level,
                ),
            )
            if alert_level in {"warning", "critical"}:
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
            conn.close()

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
            # 保留但不再告警（阈值已设为 0）
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
            conn = sqlite3.connect(self.db_path)
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
            conn.close()
        del self.active_alerts[alert_key]
        logger.info("Resolved alert: %s.%s", component, metric_name)
        return True

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

    def _generate_report_legacy(self, hours: int = 24) -> Dict[str, Any]:
        return generate_report_legacy(self, hours=hours)

    def generate_report(self, hours: int = 24) -> Dict[str, Any]:
        return generate_report(self, hours=hours)

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


_health_monitor_instance = None


def get_health_monitor(db_path: str = "health_monitor.db") -> HealthMonitor:
    global _health_monitor_instance
    if _health_monitor_instance is None:
        _health_monitor_instance = HealthMonitor(db_path)
    return _health_monitor_instance
