"""AppRuntime — start / stop / status lifecycle management."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Optional

from src.app_runtime.container import AppContainer
from src.config import get_runtime_data_path
from src.config.file_manager import close_file_config_manager
from src.monitoring.health import close_health_monitor
from src.monitoring.manager import close_monitoring_manager
from src.monitoring.runtime_task_status import RuntimeTaskState
from src.utils.event_store import close_event_store

logger = logging.getLogger(__name__)


def _call_component_method(component: Any, method_name: str, *args, **kwargs) -> Any:
    method = getattr(component, method_name, None)
    if callable(method):
        try:
            return method(*args, **kwargs)
        except Exception:
            logger.debug(
                "Failed to invoke %s on component %s",
                method_name,
                type(component).__name__,
            )
            raise
    return None


class AppRuntime:
    """Manages the lifecycle of components held by an :class:`AppContainer`.

    Separates *starting threads* from *building objects*:
    ``AppContainer`` is constructed by :func:`build_app_container`,
    then this class starts / stops all background threads.
    """

    def __init__(
        self,
        container: AppContainer,
        *,
        signal_config_loader: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.container = container
        self._signal_config_loader = signal_config_loader
        self._status: dict[str, Any] = {
            "phase": "not_started",
            "started_at": None,
            "completed_at": None,
            "duration_ms": None,
            "ready": False,
            "last_error": None,
            "steps": {},
        }

    # ── Public API ──────────────────────────────────────────────

    @property
    def status(self) -> dict[str, Any]:
        return {
            "phase": self._status["phase"],
            "started_at": self._status["started_at"],
            "completed_at": self._status["completed_at"],
            "duration_ms": self._status["duration_ms"],
            "ready": self._status["ready"],
            "last_error": self._status["last_error"],
            "steps": dict(self._status["steps"]),
        }

    def start(self) -> None:
        """Start all background threads in the correct order."""
        c = self.container
        self._status["phase"] = "starting"
        self._status["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        current_step = "initialization"
        current_started = time.monotonic()

        try:
            current_step = "runtime_mode"
            if c.storage_writer is not None:
                c.storage_writer.ensure_schema_ready()
            self._restore_trading_state()
            current_started = time.monotonic()
            controller = c.runtime_mode_controller
            if controller is None:
                raise RuntimeError("Component 'runtime_mode_controller' is None")
            controller.start()
            self._mark_step(current_step, RuntimeTaskState.READY.value, current_started)
            self._record_task_status(
                current_step,
                RuntimeTaskState.READY.value,
                current_started,
            )

            self._register_monitoring()

            self._start_notifications()

            self._status["phase"] = "running"
            self._status["ready"] = True
            self._status["completed_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            self._status["duration_ms"] = sum(
                step.get("duration_ms", 0) for step in self._status["steps"].values()
            )

            # M0.2: 启动配置摘要 —— 让排障不用翻 8 个 ini + 3 个 API 端点
            self._log_startup_summary()

        except Exception as exc:
            self._mark_step(
                current_step,
                RuntimeTaskState.FAILED.value,
                current_started,
                error=str(exc),
            )
            self._record_task_status(
                current_step,
                RuntimeTaskState.FAILED.value,
                current_started,
                error=str(exc),
            )
            self._status["phase"] = "failed"
            self._status["ready"] = False
            self._status["last_error"] = str(exc)
            logger.exception("Failed to start runtime: %s", exc)
            self.stop()
            raise

    def _log_startup_summary(self) -> None:
        """输出一行结构化启动摘要，供排障时替代翻 ini + API 三方对账。

        故意使用惰性字段读取 + 失败降级为 N/A，不能因为摘要输出本身炸掉启动。
        """
        c = self.container
        identity = c.runtime_identity
        instance = getattr(identity, "instance_name", "?")
        environment = getattr(identity, "environment", "?")
        role = getattr(identity, "instance_role", "?")

        mode = "?"
        if c.runtime_mode_controller is not None:
            try:
                current = c.runtime_mode_controller.current_mode()
                mode = getattr(current, "value", str(current)) if current else "?"
            except Exception:
                mode = "error"

        signal_config: Any | None = None
        if self._signal_config_loader is not None:
            try:
                signal_config = self._signal_config_loader()
            except Exception:
                signal_config = None

        auto_trade = "?"
        intrabar_enabled = "?"
        active_strategies: list[str] = []
        intrabar_strategies: list[str] = []
        bindings: dict[str, list[str]] = {}
        if signal_config is not None:
            auto_trade = str(getattr(signal_config, "auto_trade_enabled", "?"))
            intrabar_enabled = str(
                getattr(signal_config, "intrabar_trading_enabled", "?")
            )
            tf_map = getattr(signal_config, "strategy_timeframes", {}) or {}
            active_strategies = sorted(tf_map.keys())
            intrabar_strategies = sorted(
                getattr(signal_config, "intrabar_trading_enabled_strategies", []) or []
            )
            raw_bindings = getattr(signal_config, "account_bindings", {}) or {}
            bindings = {
                alias: sorted(strategies or [])
                for alias, strategies in raw_bindings.items()
            }

        logger.info(
            "startup_summary instance=%s env=%s role=%s mode=%s "
            "auto_trade=%s intrabar_enabled=%s "
            "active_strategies=%s intrabar_strategies=%s account_bindings=%s",
            instance,
            environment,
            role,
            mode,
            auto_trade,
            intrabar_enabled,
            active_strategies,
            intrabar_strategies,
            bindings,
        )

    def stop(self) -> None:
        """Gracefully stop all components in reverse order."""
        self._status["phase"] = "stopping"
        c = self.container

        controller = c.runtime_mode_controller
        if controller is not None:
            try:
                controller.stop()
            except Exception:
                logger.debug(
                    "Failed to stop runtime mode controller during shutdown",
                    exc_info=True,
                )

        # Calibrator: persist cache before stopping
        if c.calibrator is not None:
            try:
                calibrator_cache_path = get_runtime_data_path("calibrator_cache.json")
                os.makedirs(os.path.dirname(calibrator_cache_path), exist_ok=True)
                c.calibrator.dump(calibrator_cache_path)
                c.calibrator.stop_background_refresh()
            except Exception:
                logger.debug(
                    "Failed to dump calibrator cache during shutdown", exc_info=True
                )

        # Notifications first so its pipeline/health listeners unsubscribe
        # before we tear down the bus and health monitor below. Use close()
        # (not stop()) on app shutdown so the SQLite outbox is released too.
        if c.notification_module is not None:
            try:
                c.notification_module.close()
            except Exception:
                logger.debug(
                    "Failed to stop notification_module during shutdown",
                    exc_info=True,
                )

        # Shutdown order: signal source → execution → data
        for label, component, method in [
            ("monitoring_manager", c.monitoring_manager, "stop"),
            ("signal_runtime", c.signal_runtime, "stop"),
            ("trade_executor", c.trade_executor, "shutdown"),
            ("pending_entry_manager", c.pending_entry_manager, "shutdown"),
            ("position_manager", c.position_manager, "stop"),
            ("account_risk_state_projector", c.account_risk_state_projector, "stop"),
            ("ingestor", c.ingestor, "stop"),
            ("market_service", c.market_service, "shutdown"),
            ("indicator_manager", c.indicator_manager, "shutdown"),
            ("economic_calendar_service", c.economic_calendar_service, "stop"),
            ("storage_writer", c.storage_writer, "stop"),
        ]:
            if component is None:
                continue
            try:
                _call_component_method(component, method)
            except Exception:
                logger.debug(
                    "Failed to stop %s during shutdown",
                    label,
                    exc_info=True,
                )

        if c.health_monitor is not None:
            try:
                c.health_monitor.record_metric(
                    "system", "shutdown", 1.0, {"timestamp": "now"}
                )
            except Exception:
                logger.debug(
                    "Failed to record shutdown metric to health monitor", exc_info=True
                )

        pipeline_bus = c.pipeline_event_bus
        if pipeline_bus is not None:
            _call_component_method(pipeline_bus, "shutdown")

        for callback in reversed(list(c.shutdown_callbacks)):
            try:
                callback()
            except Exception:
                logger.debug("Failed to run runtime shutdown callback", exc_info=True)
        c.shutdown_callbacks.clear()

        indicator_manager = c.indicator_manager
        if indicator_manager is not None:
            event_store = indicator_manager.event_store
            if event_store is not None:
                try:
                    close_event_store(instance=event_store)
                except Exception:
                    logger.debug(
                        "Failed to close indicator event store during shutdown",
                        exc_info=True,
                    )

        if c.monitoring_manager is not None:
            try:
                close_monitoring_manager(instance=c.monitoring_manager)
            except Exception:
                logger.debug(
                    "Failed to close monitoring manager during shutdown", exc_info=True
                )

        if c.health_monitor is not None:
            try:
                close_health_monitor(instance=c.health_monitor)
            except Exception:
                logger.debug(
                    "Failed to close health monitor during shutdown", exc_info=True
                )

        try:
            close_file_config_manager()
        except Exception:
            logger.debug(
                "Failed to close file config manager during shutdown", exc_info=True
            )

        self._status["phase"] = "stopped"
        self._status["ready"] = False

    # ── Internal helpers ────────────────────────────────────────

    def _restore_trading_state(self) -> None:
        recovery = self.container.trading_state_recovery
        trade_module = self.container.trade_module
        if recovery is None:
            return
        try:
            recovery.warm_start()
        except Exception:
            logger.warning("Trading state warm-start failed", exc_info=True)
        if trade_module is not None:
            try:
                result = recovery.restore_trade_control(trade_module)
                if result.get("restored"):
                    logger.info("Trade control restored from persistence")
            except Exception:
                logger.warning("Trade control restore failed", exc_info=True)

    def _start_notifications(self) -> None:
        module = self.container.notification_module
        if module is None:
            return
        current_started = time.monotonic()
        try:
            module.start()
        except Exception as exc:
            logger.exception("Failed to start notification module: %s", exc)
            self._mark_step(
                "notifications",
                RuntimeTaskState.FAILED.value,
                current_started,
                error=str(exc),
            )
            self._record_task_status(
                "notifications",
                RuntimeTaskState.FAILED.value,
                current_started,
                error=str(exc),
            )
            return
        self._mark_step("notifications", RuntimeTaskState.READY.value, current_started)
        self._record_task_status(
            "notifications",
            RuntimeTaskState.READY.value,
            current_started,
        )

    def _register_monitoring(self) -> None:
        c = self.container
        if c.monitoring_manager is None:
            return
        current_started = time.monotonic()
        runtime_identity = c.runtime_identity
        is_executor = bool(
            runtime_identity is not None
            and runtime_identity.instance_role == "executor"
        )
        if not is_executor:
            c.monitoring_manager.register_component(
                "data_ingestion", c.ingestor, ["queue_stats"]
            )
            c.monitoring_manager.register_component(
                "indicator_calculation",
                c.indicator_manager,
                ["indicator_freshness", "cache_stats", "performance_stats"],
            )
            c.monitoring_manager.register_component(
                "market_data", c.market_service, ["data_latency"]
            )
            c.monitoring_manager.register_component(
                "economic_calendar", c.economic_calendar_service, ["economic_calendar"]
            )
            c.monitoring_manager.register_component(
                "signals", c.signal_runtime, ["status"]
            )
        c.monitoring_manager.register_component(
            "trading", c.trade_module, ["monitoring_summary"]
        )
        # 交易域可观测性指标
        if c.position_manager is not None:
            c.monitoring_manager.register_component(
                "position_manager",
                c.position_manager,
                ["reconciliation_lag", "position_count"],
            )
        if c.trade_executor is not None:
            c.monitoring_manager.register_component(
                "trade_executor",
                c.trade_executor,
                ["circuit_breaker", "execution_quality"],
            )
        if c.trading_state_alerts is not None:
            c.monitoring_manager.register_component(
                "trading_state",
                c.trading_state_alerts,
                ["monitoring_summary"],
            )
        if c.pending_entry_manager is not None:
            c.monitoring_manager.register_component(
                "pending_entry", c.pending_entry_manager, ["pending_entry"]
            )
        if c.account_risk_state_projector is not None:
            c.monitoring_manager.register_component(
                "account_risk_state_projection",
                c.account_risk_state_projector,
                ["status"],
            )
        c.monitoring_manager.start()
        self._mark_step("monitoring", RuntimeTaskState.READY.value, current_started)
        self._record_task_status(
            "monitoring",
            RuntimeTaskState.READY.value,
            current_started,
        )

        if c.health_monitor is not None:
            c.health_monitor.record_metric(
                "system", "startup", 1.0, {"version": "unified", "timestamp": "now"}
            )

    def _mark_step(
        self, name: str, state: str, started_at: float, *, error: Optional[str] = None
    ) -> None:
        duration_ms = int((time.monotonic() - started_at) * 1000)
        self._status["steps"][name] = {
            "state": state,
            "duration_ms": duration_ms,
            "error": error,
        }

    def _record_task_status(
        self,
        step_name: str,
        state: str,
        started_at: float,
        *,
        error: Optional[str] = None,
    ) -> None:
        from datetime import datetime, timezone

        c = self.container
        if c.storage_writer is None:
            return
        duration_ms = int((time.monotonic() - started_at) * 1000)
        success_count = 1 if state == RuntimeTaskState.READY.value else 0
        failure_count = 1 if state == RuntimeTaskState.FAILED.value else 0
        runtime_identity = c.runtime_identity
        try:
            c.storage_writer.db.write_runtime_task_status(
                [
                    (
                        "startup",
                        step_name,
                        datetime.now(timezone.utc),
                        state,
                        None,
                        None,
                        None,
                        duration_ms,
                        success_count,
                        failure_count,
                        failure_count,
                        error,
                        {"startup": True},
                        (
                            runtime_identity.instance_id
                            if runtime_identity is not None
                            else "legacy"
                        ),
                        (
                            runtime_identity.instance_role
                            if runtime_identity is not None
                            else None
                        ),
                        (
                            runtime_identity.account_key
                            if runtime_identity is not None
                            else None
                        ),
                        (
                            runtime_identity.account_alias
                            if runtime_identity is not None
                            else None
                        ),
                    )
                ]
            )
        except Exception:
            logger.debug(
                "Failed to persist startup task status for %s", step_name, exc_info=True
            )
