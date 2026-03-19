"""监控管理器

协调多个组件的健康检查任务，定期执行巡检并记录结果。
"""

import time
import threading
import logging
from typing import Any, Dict, List, Optional, Tuple

from src.config import get_shared_symbols, get_shared_timeframes

from .health_monitor import HealthMonitor, get_health_monitor

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


# 单例实例
_monitoring_manager_instance = None


def get_monitoring_manager(
    health_monitor: HealthMonitor = None,
    check_interval: int = 60
) -> MonitoringManager:
    """获取监控管理器单例"""
    global _monitoring_manager_instance
    if _monitoring_manager_instance is None:
        if health_monitor is None:
            health_monitor = get_health_monitor()
        _monitoring_manager_instance = MonitoringManager(health_monitor, check_interval)
    return _monitoring_manager_instance
