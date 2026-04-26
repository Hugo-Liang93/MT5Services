"""监控管理器

协调多个组件的健康检查任务，定期执行巡检并记录结果。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.config import get_shared_symbols, get_shared_timeframes

from .health.monitor import HealthMonitor, get_health_monitor

logger = logging.getLogger(__name__)


def _safe_get_performance_stats(component_obj: Any) -> Optional[Dict[str, Any]]:
    """调 component_obj.get_performance_stats() 并处理瞬时 race。

    indicator manager 内部有 50+ 个 dict 在 hot path 被并发 mutate，罕见快照路径
    可能与写路径在一个调度周期内竞争。race 是瞬时的，1 次重试后通常稳定。重试
    仍失败则返回 None，让调用方跳过此次指标采集——ERROR 日志降级为 DEBUG。
    """
    getter = component_obj.get_performance_stats
    try:
        return getter()
    except RuntimeError as exc:
        if "dictionary changed size" not in str(exc):
            raise
        try:
            return getter()
        except RuntimeError as exc2:
            if "dictionary changed size" in str(exc2):
                logger.debug(
                    "get_performance_stats race persisted for %s: %s",
                    getattr(component_obj, "__class__", type(component_obj)).__name__,
                    exc2,
                )
                return None
            raise


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

        logger.info(
            f"MonitoringManager initialized with check interval: {check_interval}s"
        )

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
            "methods": check_methods,
        }
        logger.info(f"Registered component for monitoring: {name}")

    @staticmethod
    def _summary_status_value(summary: Dict[str, Any]) -> float:
        # §0u P2：旧实现 row.get("count", 0) 把 TradingStateAlerts.summary() 这种
        # "行存在即代表 1 条告警" 的 schema 算成 0 → 把 failed 状态漂白成 healthy。
        # 行存在本身就是告警事实；count 缺省时按 1 计算，避免静默假阳性。
        failed = 0
        for row in list(summary.get("summary", []) or []):
            if row.get("status") == "failed":
                failed += int(row.get("count", 1) or 1)
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
            target=self._monitoring_loop, name="monitoring-manager", daemon=True
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
                            if method == "data_latency" and hasattr(
                                component_obj, "get_latest_ohlc"
                            ):
                                self._check_data_latency(component_obj, name)

                            elif method == "indicator_freshness" and hasattr(
                                component_obj, "get_snapshot"
                            ):
                                self._check_indicator_freshness(component_obj, name)

                            elif method == "queue_stats" and hasattr(
                                component_obj, "queue_stats"
                            ):
                                self.health_monitor.check_queue_stats(
                                    name, component_obj
                                )

                            elif method == "cache_stats" and hasattr(
                                component_obj, "stats"
                            ):
                                self.health_monitor.check_cache_stats(
                                    name, component_obj
                                )

                            elif method == "performance_stats" and hasattr(
                                component_obj, "get_performance_stats"
                            ):
                                stats = _safe_get_performance_stats(component_obj)
                                if stats is None:
                                    continue
                                if isinstance(stats, dict) and "success_rate" in stats:
                                    self.health_monitor.record_metric(
                                        name,
                                        "success_rate",
                                        stats["success_rate"],
                                        stats,
                                    )
                                intrabar = (
                                    stats.get("intrabar")
                                    if isinstance(stats, dict)
                                    else None
                                )
                                drop_rates = (
                                    stats.get("drop_rates")
                                    if isinstance(stats, dict)
                                    else None
                                )

                                if isinstance(intrabar, dict):
                                    if isinstance(
                                        intrabar.get("queue_age_ms_p95"), (int, float)
                                    ):
                                        self.health_monitor.record_metric(
                                            name,
                                            "intrabar_queue_age_p95_ms",
                                            float(intrabar["queue_age_ms_p95"]),
                                            stats,
                                        )
                                    if isinstance(
                                        intrabar.get("processing_latency_ms_p95"),
                                        (int, float),
                                    ):
                                        self.health_monitor.record_metric(
                                            name,
                                            "intrabar_to_decision_latency_p95_ms",
                                            float(
                                                intrabar["processing_latency_ms_p95"]
                                            ),
                                            stats,
                                        )

                                if isinstance(drop_rates, dict):
                                    drop_rate = drop_rates.get(
                                        "intrabar_queue_drop_vs_arrived_pct"
                                    )
                                    if isinstance(drop_rate, (int, float)):
                                        self.health_monitor.record_metric(
                                            name,
                                            "intrabar_drop_rate_1m",
                                            float(drop_rate),
                                            stats,
                                        )

                            elif method == "economic_calendar" and hasattr(
                                component_obj, "stats"
                            ):
                                self.health_monitor.check_economic_calendar(
                                    name, component_obj
                                )

                            elif method == "reconciliation_lag" and hasattr(
                                component_obj, "status"
                            ):
                                self._check_reconciliation_lag(component_obj, name)

                            elif method == "circuit_breaker" and hasattr(
                                component_obj, "status"
                            ):
                                self._check_circuit_breaker(component_obj, name)

                            elif method == "execution_quality" and hasattr(
                                component_obj, "status"
                            ):
                                self._check_execution_quality(component_obj, name)

                            elif method == "position_count" and hasattr(
                                component_obj, "status"
                            ):
                                pm_status = component_obj.status()
                                count = int(pm_status.get("tracked_positions", 0) or 0)
                                self.health_monitor.record_metric(
                                    name,
                                    "tracked_position_count",
                                    float(count),
                                    pm_status,
                                    check_alert=False,
                                )

                            elif method == "pending_entry" and hasattr(
                                component_obj, "status"
                            ):
                                self._check_pending_entry(component_obj, name)

                            elif method == "status" and hasattr(
                                component_obj, "status"
                            ):
                                status_payload = component_obj.status()
                                self.health_monitor.record_metric(
                                    name,
                                    "runtime_status",
                                    1.0,
                                    (
                                        status_payload
                                        if isinstance(status_payload, dict)
                                        else {"value": status_payload}
                                    ),
                                )

                            elif method == "monitoring_summary" and hasattr(
                                component_obj, "monitoring_summary"
                            ):
                                summary_payload = component_obj.monitoring_summary(
                                    hours=24
                                )
                                self.health_monitor.record_metric(
                                    name,
                                    "monitoring_summary",
                                    self._summary_status_value(summary_payload),
                                    summary_payload,
                                )

                        except Exception as e:
                            logger.error(
                                f"Failed to execute monitoring method {method} for {name}: {e}"
                            )

                report = self.health_monitor.generate_report(hours=1)
                self.health_monitor.record_metric(
                    "system",
                    "overall_status",
                    (
                        1.0
                        if report["overall_status"] == "healthy"
                        else 0.5 if report["overall_status"] == "warning" else 0.0
                    ),
                    report,
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

        indicator_component = self._monitored_components.get(
            "indicator_calculation", {}
        )
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
                    latency = self.health_monitor.check_data_latency(
                        component_name, service, symbol, timeframe
                    )
                    logger.debug(
                        f"Data latency for {symbol}/{timeframe}: {latency:.1f}s"
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to check data latency for {symbol}/{timeframe}: {e}"
                    )

    def _check_indicator_freshness(self, worker, component_name: str):
        """检查指标新鲜度（针对多个品种和时间框架）"""
        symbols, timeframes = self._resolve_monitor_targets(worker)

        for symbol in symbols:
            for timeframe in timeframes:
                try:
                    freshness = self.health_monitor.check_indicator_freshness(
                        component_name, worker, symbol, timeframe
                    )
                    logger.debug(
                        f"Indicator freshness for {symbol}/{timeframe}: {freshness:.1f}s"
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to check indicator freshness for {symbol}/{timeframe}: {e}"
                    )

    def _check_reconciliation_lag(
        self, component_obj: Any, component_name: str
    ) -> None:
        """检查持仓对账延迟。"""
        try:
            pm_status = component_obj.status()
            last_at = pm_status.get("last_reconcile_at")
            if last_at is None:
                return
            last_dt = datetime.fromisoformat(str(last_at))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            lag = (datetime.now(timezone.utc) - last_dt).total_seconds()
            self.health_monitor.record_metric(
                component_name,
                "reconciliation_lag",
                lag,
                {
                    "last_reconcile_at": str(last_at),
                    "reconcile_count": pm_status.get("reconcile_count"),
                    "tracked_positions": pm_status.get("tracked_positions"),
                    "dirty_positions": pm_status.get("dirty_positions"),
                    "last_error": pm_status.get("last_error"),
                },
            )
        except Exception as exc:
            logger.debug("Failed to check reconciliation lag: %s", exc)

    def _check_circuit_breaker(self, component_obj: Any, component_name: str) -> None:
        """检查交易熔断器状态。"""
        try:
            exec_status = component_obj.status()
            cb = exec_status.get("circuit_breaker", {})
            is_open = 1.0 if cb.get("open") else 0.0
            self.health_monitor.record_metric(
                component_name,
                "circuit_breaker_open",
                is_open,
                {
                    "consecutive_failures": cb.get("consecutive_failures"),
                    "circuit_open_at": cb.get("circuit_open_at"),
                    "health_check_failures": cb.get("health_check_failures"),
                    "max_consecutive_failures": cb.get("max_consecutive_failures"),
                },
            )
        except Exception as exc:
            logger.debug("Failed to check circuit breaker: %s", exc)

    def _check_execution_quality(self, component_obj: Any, component_name: str) -> None:
        """检查交易执行质量指标。"""
        try:
            exec_status = component_obj.status()
            eq = exec_status.get("execution_quality", {})

            # execution_count 是"成功执行数"（不含失败 dispatch）。
            total_success = int(exec_status.get("execution_count", 0) or 0)
            # 从 skip_reasons 中提取失败数（eventing.py 失败 dispatch 走 notify_skip
            # 写 execution_failed，参 §0u P2 修复）
            skip_reasons = exec_status.get("skip_reasons", {})
            failed = int(skip_reasons.get("execution_failed", 0) or 0)
            # §0u P2：分母用"尝试数 = 成功 + 失败"而非仅成功数。旧实现 if total > 0
            # 在 "execution_count=0 + 全失败" 极端场景把 failure_rate 静默丢弃。
            total_attempts = total_success + failed
            if total_attempts > 0:
                failure_rate = failed / total_attempts
                self.health_monitor.record_metric(
                    component_name,
                    "execution_failure_rate",
                    failure_rate,
                    {
                        "total_attempts": total_attempts,
                        "total_executed": total_success,
                        "failed": failed,
                        "signals_received": exec_status.get("signals_received"),
                        "signals_passed": exec_status.get("signals_passed"),
                    },
                )

            overflows = int(eq.get("queue_overflows", 0) or 0)
            if overflows > 0:
                self.health_monitor.record_metric(
                    component_name,
                    "execution_queue_overflows",
                    float(overflows),
                    eq,
                )
        except Exception as exc:
            logger.debug("Failed to check execution quality: %s", exc)

    @staticmethod
    def _thread_alive(component_obj: Any, attr: str) -> bool:
        thread = getattr(component_obj, attr, None)
        if thread is None:
            return False
        is_alive = getattr(thread, "is_alive", None)
        if not callable(is_alive):
            return False
        try:
            return bool(is_alive())
        except Exception:
            return False

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

            # §0w P2：旧实现仅探 _monitor_thread → fill worker 挂掉时仍报存活
            # PendingEntryManager.is_running() 是 monitor + fill worker 双线程
            # 必须都活着的正式 SSOT；以它为准记录聚合 pending_runtime_down 指标
            # （down=1, up=0；与 circuit_breaker_open 同方向，可走 critical 阈值）。
            # 仍保留 pending_monitor_alive / pending_fill_worker_alive 两条
            # 细粒度指标供 dashboard 排障。
            monitor_alive = bool(self._thread_alive(pending_mgr, "_monitor_thread"))
            fill_alive = bool(self._thread_alive(pending_mgr, "_fill_worker_thread"))
            running_getter = getattr(pending_mgr, "is_running", None)
            if callable(running_getter):
                try:
                    overall_running = bool(running_getter())
                except Exception as exc:
                    logger.warning(
                        "PendingEntryManager.is_running() raised %s; "
                        "falling back to per-thread aggregation: %s",
                        type(exc).__name__,
                        exc,
                    )
                    overall_running = monitor_alive and fill_alive
            else:
                overall_running = monitor_alive and fill_alive

            self.health_monitor.record_metric(
                component_name,
                "pending_monitor_alive",
                1.0 if monitor_alive else 0.0,
            )
            self.health_monitor.record_metric(
                component_name,
                "pending_fill_worker_alive",
                1.0 if fill_alive else 0.0,
            )
            self.health_monitor.record_metric(
                component_name,
                "pending_runtime_down",
                0.0 if overall_running else 1.0,
            )
            if not overall_running:
                logger.warning(
                    "PendingEntryManager runtime down (monitor_alive=%s, fill_alive=%s)",
                    monitor_alive,
                    fill_alive,
                )

        except Exception as e:
            logger.error("Failed to check pending entry health: %s", e)


_monitoring_manager_instances: Dict[tuple[int, int], MonitoringManager] = {}


def get_monitoring_manager(
    health_monitor: HealthMonitor = None, check_interval: int = 60
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
            key
            for key, value in _monitoring_manager_instances.items()
            if value is instance
        )
    elif health_monitor is not None:
        if check_interval is None:
            keys_to_close.extend(
                key
                for key in _monitoring_manager_instances
                if key[0] == id(health_monitor)
            )
        else:
            keys_to_close.append((id(health_monitor), int(check_interval)))

    for key in keys_to_close:
        manager = _monitoring_manager_instances.pop(key, None)
        if manager is not None:
            manager.stop()
