"""
Lightweight health monitoring with in-memory ring-buffer metrics and
SQLite-backed alert history.

v2 改造（相比 v1）：
- health_metrics / system_status 表 → 纯内存 MetricsStore（deque 环形缓冲）
- 消除 ~300 万条/天 SQLite 写入和 3 GB+ 文件膨胀
- alert_history 保留 SQLite（写入频率极低，天级别几十条）
- 后台刷盘线程 → 移除（无 SQLite 指标写入需求）
- 对外接口完全不变：record_metric / generate_report / get_recent_metrics / ...
- NullHealthMonitor：回测 / 测试用的空实现，零 I/O
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .checks import (
    check_cache_stats,
    check_data_latency,
    check_economic_calendar,
    check_indicator_freshness,
    check_queue_stats,
)
from .common import is_finite_metric_value
from .metrics_store import MetricsStore
from .reporting import generate_report, get_recent_metrics, get_system_status

logger = logging.getLogger(__name__)


class HealthMonitor:
    """In-memory ring-buffer backed runtime health monitor.

    Metrics are stored in ``MetricsStore`` (collections.deque per key).
    Only ``alert_history`` is persisted to a lightweight SQLite file.
    """

    def __init__(
        self, db_path: str = "health_monitor.db", ring_size: int = 2400
    ) -> None:
        # db_path 现在仅用于 alert_history SQLite
        # 文件名改为 health_alerts.db 以反映实际用途
        alert_dir = os.path.dirname(os.path.abspath(db_path))
        self._alert_db_path = os.path.join(alert_dir, "health_alerts.db")

        self._lock = threading.Lock()
        # 启动时间戳：用于抑制"数据尚未补齐"的瞬态 alert。
        # 场景：启动瞬间 economic_calendar / indicator_freshness 可能因为数据还在拉回
        # 而显示 staleness 很大，但系统会在启动后几秒内自动 refresh。
        # 若在这段 grace 期触发 alert，会污染告警历史并产生假阳性。
        self._started_at_monotonic = time.monotonic()
        # Grace 期内被豁免的 alert metric 名称（启动 60 秒内不触发）。
        # 仅包含"启动瞬态"指标 —— 启动瞬间因数据尚未刷回导致看起来异常，
        # refresh 完成后会自然回归。
        # 不包含 data_latency / queue_depth / intrabar_* 等"运行时数据流"指标，
        # 这些即使在启动期异常也应立即告警。
        self._startup_grace_seconds = 60.0
        self._startup_grace_metrics = frozenset(
            {
                "economic_calendar_staleness",
                "indicator_freshness",
            }
        )

        # 核心数据存储（纯内存）
        self._store = MetricsStore(
            ring_size=ring_size,
            alert_db_path=self._alert_db_path,
        )

        # 内存缓存：最近 100 条/key（兼容旧 API：get_recent_metrics 优先读此缓存）
        self.metrics: Dict[str, List[Dict[str, Any]]] = {}

        # 告警阈值配置
        self.alerts: Dict[str, Dict[str, float]] = {
            "data_latency": {"warning": 10.0, "critical": 30.0},
            "indicator_freshness": {"warning": 60.0, "critical": 300.0},
            "queue_depth": {"warning": 1000, "critical": 5000},
            "indicator_compute_p99_ms": {"warning": 500.0, "critical": 2000.0},
            "intrabar_drop_rate_1m": {"warning": 1.0, "critical": 5.0},
            "intrabar_queue_age_p95_ms": {"warning": 2500.0, "critical": 5000.0},
            "intrabar_to_decision_latency_p95_ms": {
                "warning": 3500.0,
                "critical": 7000.0,
            },
            "cache_hit_rate": {"warning": 0.0, "critical": 0.0},
            # 交易域指标
            "reconciliation_lag": {"warning": 30.0, "critical": 120.0},
            "circuit_breaker_open": {"warning": 0.5, "critical": 0.5},
            "execution_failure_rate": {"warning": 0.1, "critical": 0.3},
            "execution_queue_overflows": {"warning": 1.0, "critical": 5.0},
        }
        self.active_alerts: Dict[str, Dict[str, Any]] = {}

        # Optional external listener invoked when a warning/critical alert
        # transitions into active_alerts. Set via set_alert_listener(). Kept
        # minimal so this module stays independent of notifications/.
        self._alert_listener: Optional[Callable[[Dict[str, Any]], None]] = None

        # Report cache: avoid repeated full-ring scans from SSE polling
        self._report_cache: Optional[Dict[str, Any]] = None
        self._report_cache_at: float = 0.0
        self._report_cache_ttl: float = 15.0  # seconds

        logger.info(
            "HealthMonitor initialized (in-memory ring_size=%d, alerts_db=%s)",
            ring_size,
            self._alert_db_path,
        )

    # ─── 配置 ────────────────────────────────────────────────────────────────

    def configure_alerts(
        self,
        *,
        data_latency_warning: Optional[float] = None,
        data_latency_critical: Optional[float] = None,
        intrabar_drop_rate_1m_warning: Optional[float] = None,
        intrabar_drop_rate_1m_critical: Optional[float] = None,
        intrabar_queue_age_p95_ms_warning: Optional[float] = None,
        intrabar_queue_age_p95_ms_critical: Optional[float] = None,
        intrabar_to_decision_latency_p95_ms_warning: Optional[float] = None,
        intrabar_to_decision_latency_p95_ms_critical: Optional[float] = None,
    ) -> None:
        if data_latency_warning is not None:
            self.alerts["data_latency"]["warning"] = float(data_latency_warning)
        if data_latency_critical is not None:
            self.alerts["data_latency"]["critical"] = float(data_latency_critical)
        if intrabar_drop_rate_1m_warning is not None:
            self.alerts["intrabar_drop_rate_1m"]["warning"] = float(
                intrabar_drop_rate_1m_warning
            )
        if intrabar_drop_rate_1m_critical is not None:
            self.alerts["intrabar_drop_rate_1m"]["critical"] = float(
                intrabar_drop_rate_1m_critical
            )
        if intrabar_queue_age_p95_ms_warning is not None:
            self.alerts["intrabar_queue_age_p95_ms"]["warning"] = float(
                intrabar_queue_age_p95_ms_warning
            )
        if intrabar_queue_age_p95_ms_critical is not None:
            self.alerts["intrabar_queue_age_p95_ms"]["critical"] = float(
                intrabar_queue_age_p95_ms_critical
            )
        if intrabar_to_decision_latency_p95_ms_warning is not None:
            self.alerts["intrabar_to_decision_latency_p95_ms"]["warning"] = float(
                intrabar_to_decision_latency_p95_ms_warning
            )
        if intrabar_to_decision_latency_p95_ms_critical is not None:
            self.alerts["intrabar_to_decision_latency_p95_ms"]["critical"] = float(
                intrabar_to_decision_latency_p95_ms_critical
            )

    def set_alert_listener(
        self, listener: Optional[Callable[[Dict[str, Any]], None]]
    ) -> None:
        """Register (or clear with ``None``) a callback for warning/critical alerts.

        Intended for downstream notification sinks. Called synchronously from
        :meth:`record_metric` with a copy of the alert payload dict; the
        callback MUST be non-blocking. Exceptions are caught and logged.
        """
        self._alert_listener = listener

    # ─── 核心写入 ────────────────────────────────────────────────────────────

    def record_metric(
        self,
        component: str,
        metric_name: str,
        value: float,
        details: Dict[str, Any] | None = None,
        check_alert: bool = True,
    ) -> None:
        if not is_finite_metric_value(value):
            logger.debug(
                "Skipping non-finite metric: %s.%s=%r", component, metric_name, value
            )
            return

        timestamp = self._utc_now().isoformat()
        metric_value = float(value)
        alert_level = (
            self._check_alert_level(component, metric_name, metric_value)
            if check_alert
            else None
        )

        # 1. 内存快查缓存（最后 100 条 per key）
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

        # 2. 环形缓冲（用于 generate_report 窗口聚合）
        details_json = json.dumps(details) if details else None
        self._store.append(
            component=component,
            metric_name=metric_name,
            value=metric_value,
            timestamp_iso=timestamp,
            details_json=details_json,
            alert_level=alert_level,
        )

        # 3. 告警写入 SQLite（频率极低）
        if alert_level in {"warning", "critical"}:
            threshold = self.alerts[metric_name][alert_level]
            message = self._generate_alert_message(
                component,
                metric_name,
                alert_level,
                metric_value,
                threshold,
            )
            self._store.write_alert(
                timestamp=timestamp,
                component=component,
                metric_name=metric_name,
                alert_level=alert_level,
                value=metric_value,
                threshold=threshold,
                message=message,
            )
            alert_key = f"{component}.{metric_name}"
            alert_payload = {
                "timestamp": timestamp,
                "component": component,
                "metric_name": metric_name,
                "alert_level": alert_level,
                "value": metric_value,
                "threshold": threshold,
                "message": message,
            }
            self.active_alerts[alert_key] = alert_payload
            logger.warning("New alert: %s", message)
            listener = self._alert_listener
            if listener is not None:
                try:
                    listener(dict(alert_payload))
                except Exception:
                    # Isolate listener failure — losing a notification must
                    # never corrupt the health monitor's own state.
                    logger.exception("health alert listener failed")

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
        # 启动 grace 期：启动不足 _startup_grace_seconds 秒时，
        # 豁免 "启动瞬态" 类指标的 alert（等 refresh 完成后自然回归正常）。
        # 仅影响 alert 生成，metric 本身仍正常记录到 ring buffer（便于事后审计）。
        if metric_name in self._startup_grace_metrics and (
            time.monotonic() - self._started_at_monotonic < self._startup_grace_seconds
        ):
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
        elif metric_name in {
            "intrabar_drop_rate_1m",
            "intrabar_queue_age_p95_ms",
            "intrabar_to_decision_latency_p95_ms",
        }:
            if value >= thresholds["critical"]:
                return "critical"
            if value >= thresholds["warning"]:
                return "warning"
        elif metric_name in {
            "reconciliation_lag",
            "circuit_breaker_open",
            "execution_failure_rate",
            "execution_queue_overflows",
        }:
            if value >= thresholds["critical"]:
                return "critical"
            if value >= thresholds["warning"]:
                return "warning"
        elif metric_name == "cache_hit_rate":
            pass
        return None

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
        if metric_name == "intrabar_drop_rate_1m":
            return (
                f"{component}: intrabar drop rate {alert_level} - "
                f"current={value:.2f}% threshold={threshold:.2f}% (1m)"
            )
        if metric_name == "intrabar_queue_age_p95_ms":
            return (
                f"{component}: intrabar queue age P95 {alert_level} - "
                f"current={value:.0f}ms threshold={threshold:.0f}ms"
            )
        if metric_name == "intrabar_to_decision_latency_p95_ms":
            return (
                f"{component}: intrabar to-decision latency P95 {alert_level} - "
                f"current={value:.0f}ms threshold={threshold:.0f}ms"
            )
        if metric_name == "cache_hit_rate":
            return (
                f"{component}: cache hit rate {alert_level} - "
                f"current={value:.1%} threshold={threshold:.1%}"
            )
        if metric_name == "reconciliation_lag":
            return (
                f"{component}: reconciliation lag {alert_level} - "
                f"current={value:.1f}s threshold={threshold:.0f}s"
            )
        if metric_name == "circuit_breaker_open":
            return (
                f"{component}: circuit breaker OPEN {alert_level} - "
                f"auto-trading suspended, manual reset required"
            )
        if metric_name == "execution_failure_rate":
            return (
                f"{component}: execution failure rate {alert_level} - "
                f"current={value:.1%} threshold={threshold:.1%}"
            )
        if metric_name == "execution_queue_overflows":
            return (
                f"{component}: execution queue overflows {alert_level} - "
                f"current={value:.0f} threshold={threshold:.0f} "
                f"(signals may be permanently lost)"
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
        self._store.resolve_alert(component, metric_name, resolved_by)
        del self.active_alerts[alert_key]
        logger.info("Resolved alert: %s.%s", component, metric_name)
        return True

    # ─── 代理检查方法（委托给 health_checks 模块） ─────────────────────────

    def check_data_latency(
        self, component: str, service: Any, symbol: str, timeframe: str
    ) -> float:
        return check_data_latency(self, component, service, symbol, timeframe)

    def check_indicator_freshness(
        self,
        component: str,
        worker: Any,
        symbol: str,
        timeframe: str,
    ) -> float:
        return check_indicator_freshness(self, component, worker, symbol, timeframe)

    def check_queue_stats(self, component: str, ingestor: Any) -> Dict[str, Any]:
        return check_queue_stats(self, component, ingestor)

    def check_cache_stats(self, component: str, worker: Any) -> Dict[str, Any]:
        return check_cache_stats(self, component, worker)

    def check_economic_calendar(self, component: str, service: Any) -> Dict[str, Any]:
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
        """Clean up old alert history. Ring buffers are self-managing."""
        deleted = self._store.cleanup_alerts(days_to_keep)
        if deleted:
            logger.info("Cleaned up %s old resolved alerts", deleted)

    def get_system_status(self) -> Dict[str, Any]:
        return get_system_status(self)

    # ─── 生命周期 ────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close alert SQLite connection. Ring buffers are GC'd."""
        self._store.close()


# ─── No-op 实现（回测 / 测试专用）─────────────────────────────────────────────


class NullHealthMonitor:
    """回测 / 测试用的空实现，满足 HealthMonitor 的全部接口但不做任何 I/O。"""

    def __init__(self) -> None:
        self.metrics: Dict[str, List[Dict[str, Any]]] = {}
        self.alerts: Dict[str, Any] = {}
        self.active_alerts: Dict[str, Any] = {}

    def configure_alerts(self, **kwargs: Any) -> None:
        pass

    def set_alert_listener(
        self, listener: Optional[Callable[[Dict[str, Any]], None]]
    ) -> None:
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

    def resolve_alert(
        self, component: str, metric_name: str, resolved_by: str = "system"
    ) -> bool:
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
