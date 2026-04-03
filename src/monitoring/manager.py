"""监控管理器

协调多个组件的健康检查任务，定期执行巡检并记录结果。
"""

import time
import threading
import logging
from typing import Any, Dict, List, Optional, Tuple

from src.config import get_shared_symbols, get_shared_timeframes

from .health.monitor import HealthMonitor, get_health_monitor

logger = logging.getLogger(__name__)


class MonitoringManager:
    """监控管理器，协调多个监控任务"""

    def __init__(self, health_monitor: HealthMonitor, check_interval: int = 60):
        self.health_monitor = health_monitor
        self.check_interval = check_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._monitored_components = {}
        self._last_retention_at = 0.0
        self.retention_interval_seconds = 6 * 3600
        self.health_retention_days = 30
        self.event_retention_days = 7

        logger.info(f"MonitoringManager initialized with check interval: {check_interval}s")

    def register_component(self, name: str, component_obj, check_methods: List[str]):
        """
        注册监控组件

        Args:
            name: 组件名称
            component_obj: 组件对象
            check_methods: 检查方法列表（如 ["data_latency", "queue_stats"]）
        """
        self._monitored_components[name] = {
            "obj": component_obj,
            "methods": check_methods
        }
        logger.info(f"Registered component for monitoring: {name}")

    @staticmethod
    def _summary_status_value(summary: Dict[str, Any]) -> float:
        failed = 0
        for row in list(summary.get("summary", []) or []):
            if row.get("status") == "failed":
                failed += int(row.get("count", 0) or 0)
        return 0.5 if failed > 0 else 1.0

    def list_registered_components(self) -> List[Dict[str, Any]]:
        """返回已注册组件快照，供监控 API 展示。"""
        rows: List[Dict[str, Any]] = []
        for name, component_info in self._monitored_components.items():
            rows.append(
                {
                    "name": name,
                    "methods": list(component_info.get("methods", [])),
                    "enabled": True,
                }
            )
        return sorted(rows, key=lambda item: item["name"])

    def start(self):
        """启动监控管理器"""
        if self._thread and self._thread.is_alive():
            logger.warning("MonitoringManager already running")
            return

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._monitoring_loop,
            name="monitoring-manager",
            daemon=True
        )
        self._thread.start()
        logger.info("MonitoringManager started")

    def stop(self):
        """停止监控管理器"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("MonitoringManager stopped")

    @staticmethod
    def _resolve_monitor_targets(component_obj) -> Tuple[List[str], List[str]]:
        config = getattr(component_obj, "config", None)
        if config is not None:
            symbols = list(getattr(config, "symbols", []) or [])
            timeframes = list(getattr(config, "timeframes", []) or [])
            if symbols and timeframes:
                return symbols, timeframes

        symbols = list(get_shared_symbols() or [])
        timeframes = list(get_shared_timeframes() or [])
        return symbols or ["XAUUSD"], timeframes or ["M1"]

    def _monitoring_loop(self):
        """监控循环"""
        logger.info("Monitoring loop started")

        while not self._stop.is_set():
            try:
                for name, component_info in self._monitored_components.items():
                    component_obj = component_info["obj"]
                    methods = component_info["methods"]

                    for method in methods:
                        try:
                            if method == "data_latency" and hasattr(component_obj, "get_latest_ohlc"):
                                self._check_data_latency(component_obj, name)

                            elif method == "indicator_freshness" and hasattr(component_obj, "get_snapshot"):
                                self._check_indicator_freshness(component_obj, name)

                            elif method == "queue_stats" and hasattr(component_obj, "queue_stats"):
                                self.health_monitor.check_queue_stats(name, component_obj)

                            elif method == "cache_stats" and hasattr(component_obj, "stats"):
                                self.health_monitor.check_cache_stats(name, component_obj)

                            elif method == "performance_stats" and hasattr(component_obj, "get_performance_stats"):
                                stats = component_obj.get_performance_stats()
                                if "success_rate" in stats:
                                    self.health_monitor.record_metric(
                                        name,
                                        "success_rate",
                                        stats["success_rate"],
                                        stats
                                    )

                            elif method == "economic_calendar" and hasattr(component_obj, "stats"):
                                self.health_monitor.check_economic_calendar(name, component_obj)

                            elif method == "pending_entry" and hasattr(component_obj, "status"):
                                self._check_pending_entry(component_obj, name)

                            elif method == "status" and hasattr(component_obj, "status"):
                                status_payload = component_obj.status()
                                self.health_monitor.record_metric(
                                    name,
                                    "runtime_status",
                                    1.0,
                                    status_payload if isinstance(status_payload, dict) else {"value": status_payload},
                                )

                            elif method == "monitoring_summary" and hasattr(component_obj, "monitoring_summary"):
                                summary_payload = component_obj.monitoring_summary(hours=24)
                                self.health_monitor.record_metric(
                                    name,
                                    "monitoring_summary",
                                    self._summary_status_value(summary_payload),
                                    summary_payload,
                                )

                        except Exception as e:
                            logger.error(f"Failed to execute monitoring method {method} for {name}: {e}")

                report = self.health_monitor.generate_report(hours=1)
                self.health_monitor.record_metric(
                    "system",
                    "overall_status",
                    1.0 if report["overall_status"] == "healthy" else
                    0.5 if report["overall_status"] == "warning" else 0.0,
                    report
                )
                self._run_retention_if_due()

                self._stop.wait(self.check_interval)

            except Exception as e:
                logger.exception(f"Error in monitoring loop: {e}")
                time.sleep(self.check_interval)

    def _run_retention_if_due(self) -> None:
        now = time.time()
        if now - self._last_retention_at < self.retention_interval_seconds:
            return
        self._last_retention_at = now

        try:
            self.health_monitor.cleanup_old_data(self.health_retention_days)
        except Exception as exc:
            logger.error("Failed to cleanup health monitor data: %s", exc)

        indicator_component = self._monitored_components.get("indicator_calculation", {})
        indicator_obj = indicator_component.get("obj")
        if indicator_obj is not None and hasattr(indicator_obj, "cleanup_old_events"):
            try:
                indicator_obj.cleanup_old_events(self.event_retention_days)
            except Exception as exc:
                logger.error("Failed to cleanup old indicator events: %s", exc)

    def _check_data_latency(self, service, component_name: str):
        """检查数据延迟（针对多个品种和时间框架）"""
        symbols, timeframes = self._resolve_monitor_targets(service)

        for symbol in symbols:
            for timeframe in timeframes:
                try:
                    latency = self.health_monitor.check_data_latency(component_name, service, symbol, timeframe)
                    logger.debug(f"Data latency for {symbol}/{timeframe}: {latency:.1f}s")
                except Exception as e:
                    logger.error(f"Failed to check data latency for {symbol}/{timeframe}: {e}")

    def _check_indicator_freshness(self, worker, component_name: str):
        """检查指标新鲜度（针对多个品种和时间框架）"""
        symbols, timeframes = self._resolve_monitor_targets(worker)

        for symbol in symbols:
            for timeframe in timeframes:
                try:
                    freshness = self.health_monitor.check_indicator_freshness(component_name, worker, symbol, timeframe)
                    logger.debug(f"Indicator freshness for {symbol}/{timeframe}: {freshness:.1f}s")
                except Exception as e:
                    logger.error(f"Failed to check indicator freshness for {symbol}/{timeframe}: {e}")

    def _check_pending_entry(self, pending_mgr, component_name: str):
        """检查 PendingEntryManager 健康状态。"""
        try:
            status = pending_mgr.status()
            stats = status.get("stats", {})
            active = status.get("active_count", 0)
            filled = stats.get("total_filled", 0)
            expired = stats.get("total_expired", 0)
            submitted = stats.get("total_submitted", 0)

            # 记录活跃 pending 数量
            self.health_monitor.record_metric(
                component_name,
                "pending_active_count",
                float(active),
                {"filled": filled, "expired": expired, "submitted": submitted},
            )

            # 记录 fill_rate
            fill_rate = stats.get("fill_rate")
            if fill_rate is not None:
                self.health_monitor.record_metric(
                    component_name,
                    "pending_fill_rate",
                    float(fill_rate),
                    {"submitted": submitted, "filled": filled},
                )

            # monitor 线程存活检查
            monitor_alive = (
                hasattr(pending_mgr, "_monitor_thread")
                and pending_mgr._monitor_thread is not None
                and pending_mgr._monitor_thread.is_alive()
            )
            self.health_monitor.record_metric(
                component_name,
                "pending_monitor_alive",
                1.0 if monitor_alive else 0.0,
            )
            if not monitor_alive:
                logger.warning("PendingEntryManager monitor thread is not alive")

        except Exception as e:
            logger.error("Failed to check pending entry health: %s", e)


_monitoring_manager_instances: Dict[tuple[int, int], MonitoringManager] = {}


def get_monitoring_manager(
    health_monitor: HealthMonitor = None,
    check_interval: int = 60
) -> MonitoringManager:
    """获取与指定监控器绑定的 MonitoringManager 实例。"""
    if health_monitor is None:
        health_monitor = get_health_monitor()
    key = (id(health_monitor), int(check_interval))
    instance = _monitoring_manager_instances.get(key)
    if instance is None:
        instance = MonitoringManager(health_monitor, check_interval)
        _monitoring_manager_instances[key] = instance
    return instance


def close_monitoring_manager(
    *,
    instance: MonitoringManager | None = None,
    health_monitor: HealthMonitor | None = None,
    check_interval: int | None = None,
) -> None:
    keys_to_close: list[tuple[int, int]] = []
    if instance is not None:
        keys_to_close.extend(
            key for key, value in _monitoring_manager_instances.items() if value is instance
        )
    elif health_monitor is not None:
        if check_interval is None:
            keys_to_close.extend(
                key for key in _monitoring_manager_instances if key[0] == id(health_monitor)
            )
        else:
            keys_to_close.append((id(health_monitor), int(check_interval)))

    for key in keys_to_close:
        manager = _monitoring_manager_instances.pop(key, None)
        if manager is not None:
            manager.stop()
