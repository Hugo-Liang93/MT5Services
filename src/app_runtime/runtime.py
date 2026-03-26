"""AppRuntime — start / stop / status lifecycle management."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Optional

from src.app_runtime.container import AppContainer

logger = logging.getLogger(__name__)

_CALIBRATOR_CACHE_PATH = "data/calibrator_cache.json"


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
            for current_step, starter in [
                ("storage", c.storage_writer.start),
                ("indicators", c.indicator_manager.start),
                ("ingestion", c.ingestor.start),
                ("economic_calendar", c.economic_calendar_service.start),
                ("signals", c.signal_runtime.start),
            ]:
                current_started = time.monotonic()
                starter()
                self._mark_step(current_step, "ready", current_started)
                self._record_task_status(current_step, "ready", current_started)

            self._start_calibrator()
            self._start_performance_tracker()
            self._start_pending_entry()
            self._start_position_manager()
            self._register_monitoring()

            self._status["phase"] = "running"
            self._status["ready"] = True
            self._status["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._status["duration_ms"] = sum(
                step.get("duration_ms", 0) for step in self._status["steps"].values()
            )

        except Exception as exc:
            self._mark_step(current_step, "failed", current_started, error=str(exc))
            self._record_task_status(current_step, "failed", current_started, error=str(exc))
            self._status["phase"] = "failed"
            self._status["ready"] = False
            self._status["last_error"] = str(exc)
            logger.exception("Failed to start runtime: %s", exc)
            self.stop()
            raise

    def stop(self) -> None:
        """Gracefully stop all components in reverse order."""
        self._status["phase"] = "stopping"
        c = self.container

        # Calibrator: persist cache before stopping
        if c.calibrator is not None:
            try:
                os.makedirs("data", exist_ok=True)
                c.calibrator.dump(_CALIBRATOR_CACHE_PATH)
                c.calibrator.stop_background_refresh()
            except Exception:
                logger.debug("Failed to dump calibrator cache during shutdown", exc_info=True)

        # Shutdown order: signal source → execution → data
        for label, component, method in [
            ("monitoring_manager", c.monitoring_manager, "stop"),
            ("signal_runtime", c.signal_runtime, "stop"),
            ("trade_executor", c.trade_executor, "shutdown"),
            ("pending_entry_manager", c.pending_entry_manager, "shutdown"),
            ("position_manager", c.position_manager, "stop"),
            ("ingestor", c.ingestor, "stop"),
            ("market_service", c.market_service, "shutdown"),
            ("indicator_manager", c.indicator_manager, "shutdown"),
            ("economic_calendar_service", c.economic_calendar_service, "stop"),
            ("storage_writer", c.storage_writer, "stop"),
        ]:
            if component is None:
                continue
            try:
                getattr(component, method)()
            except Exception:
                logger.debug("Failed to stop %s during shutdown", label, exc_info=True)

        if c.health_monitor is not None:
            try:
                c.health_monitor.record_metric(
                    "system", "shutdown", 1.0, {"timestamp": "now"}
                )
            except Exception:
                pass

        pipeline_bus = getattr(c, "pipeline_event_bus", None)
        if pipeline_bus is not None:
            pipeline_bus.shutdown()

        self._status["phase"] = "stopped"
        self._status["ready"] = False

    # ── Internal helpers ────────────────────────────────────────

    def _start_calibrator(self) -> None:
        calibrator = self.container.calibrator
        if calibrator is None:
            return
        try:
            os.makedirs("data", exist_ok=True)
            loaded = calibrator.load(_CALIBRATOR_CACHE_PATH)
            logger.info("Calibrator: loaded %d cache entries from disk", loaded)
        except Exception:
            logger.debug("Calibrator: warm-start load failed (non-fatal)", exc_info=True)
        calibrator.start_background_refresh()

    def _start_performance_tracker(self) -> None:
        """从 DB 恢复 PerformanceTracker 的日内统计，防止重启导致学习归零。

        查询过去 24 小时的 signal_outcomes + trade_outcomes，按时间升序重放。
        非致命：DB 不可用时仅记录 debug 日志，不影响启动流程。
        """
        perf = self.container.performance_tracker
        if perf is None:
            return
        sw = self.container.storage_writer
        if sw is None:
            return
        try:
            rows = sw.db.signal_repo.fetch_recent_outcomes(hours=24)
            perf.warm_up_from_db(rows)
        except Exception:
            logger.debug(
                "PerformanceTracker: warm-up from DB failed (non-fatal)", exc_info=True
            )

    def _start_pending_entry(self) -> None:
        if self.container.pending_entry_manager is not None:
            self.container.pending_entry_manager.start()

    def _start_position_manager(self) -> None:
        c = self.container
        if c.position_manager is None:
            return
        current_started = time.monotonic()
        reconcile_interval = 60
        if self._signal_config_loader is not None:
            try:
                reconcile_interval = self._signal_config_loader().position_reconcile_interval
            except Exception:
                pass
        c.position_manager.start(reconcile_interval=reconcile_interval)
        try:
            recovery_result = c.position_manager.sync_open_positions()
            logger.info("PositionManager startup sync: %s", recovery_result)
        except Exception:
            logger.warning("PositionManager startup sync failed", exc_info=True)
        self._mark_step("position_manager", "ready", current_started)

    def _register_monitoring(self) -> None:
        c = self.container
        if c.monitoring_manager is None:
            return
        current_started = time.monotonic()
        c.monitoring_manager.register_component("data_ingestion", c.ingestor, ["queue_stats"])
        c.monitoring_manager.register_component(
            "indicator_calculation",
            c.indicator_manager,
            ["indicator_freshness", "cache_stats", "performance_stats"],
        )
        c.monitoring_manager.register_component("market_data", c.market_service, ["data_latency"])
        c.monitoring_manager.register_component(
            "economic_calendar", c.economic_calendar_service, ["economic_calendar"]
        )
        c.monitoring_manager.register_component("signals", c.signal_runtime, ["status"])
        if c.pending_entry_manager is not None:
            c.monitoring_manager.register_component(
                "pending_entry", c.pending_entry_manager, ["pending_entry"]
            )
        c.monitoring_manager.start()
        self._mark_step("monitoring", "ready", current_started)
        self._record_task_status("monitoring", "ready", current_started)

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
        self, step_name: str, state: str, started_at: float, *, error: Optional[str] = None
    ) -> None:
        from datetime import datetime, timezone

        c = self.container
        if c.storage_writer is None:
            return
        duration_ms = int((time.monotonic() - started_at) * 1000)
        success_count = 1 if state == "ready" else 0
        failure_count = 1 if state == "failed" else 0
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
                    )
                ]
            )
        except Exception:
            logger.debug(
                "Failed to persist startup task status for %s", step_name, exc_info=True
            )
