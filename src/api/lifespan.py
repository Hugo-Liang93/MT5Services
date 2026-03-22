from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


_CALIBRATOR_CACHE_PATH = "data/calibrator_cache.json"


def shutdown_components(container: Any) -> None:
    # Calibrator 热停止：先持久化缓存，再停后台刷新线程。
    calibrator = getattr(container, "calibrator", None)
    if calibrator is not None:
        try:
            import os
            os.makedirs("data", exist_ok=True)
            calibrator.dump(_CALIBRATOR_CACHE_PATH)
            calibrator.stop_background_refresh()
        except Exception:
            logger.debug("Failed to dump calibrator cache during shutdown", exc_info=True)

    for label, component, method in [
        ("monitoring_manager", container.monitoring_manager, "stop"),
        ("position_manager", container.position_manager, "stop"),
        ("trade_executor", container.trade_executor, "shutdown"),
        ("signal_runtime", container.signal_runtime, "stop"),
        ("indicator_manager", container.indicator_manager, "shutdown"),
        ("economic_calendar_service", container.economic_calendar_service, "stop"),
        ("ingestor", container.ingestor, "stop"),
        ("storage_writer", container.storage_writer, "stop"),
    ]:
        if component is None:
            continue
        try:
            getattr(component, method)()
        except Exception:
            logger.debug("Failed to stop %s during shutdown", label, exc_info=True)


def create_lifespan(
    *,
    container: Any,
    startup_status: dict[str, Any],
    ensure_initialized: Callable[[], None],
    reset_startup_status: Callable[[], None],
    mark_startup_step: Callable[[str, str, float, str | None], None],
    record_runtime_task_status: Callable[[str, str, str, int, str | None], None],
    signal_config_loader: Callable[[], Any],
):
    @asynccontextmanager
    async def lifespan(_app):
        ensure_initialized()
        reset_startup_status()
        startup_status["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        current_step = "initialization"
        current_started = time.monotonic()

        try:
            for current_step, starter in [
                ("storage", container.storage_writer.start),
                ("ingestion", container.ingestor.start),
                ("economic_calendar", container.economic_calendar_service.start),
                ("indicators", container.indicator_manager.start),
                ("signals", container.signal_runtime.start),
            ]:
                current_started = time.monotonic()
                starter()
                mark_startup_step(current_step, "ready", current_started)
                record_runtime_task_status(
                    "startup",
                    current_step,
                    "ready",
                    startup_status["steps"][current_step]["duration_ms"],
                )

            # Calibrator 热启动：从上次 dump 的缓存恢复胜率数据，避免冷启动。
            calibrator = getattr(container, "calibrator", None)
            if calibrator is not None:
                try:
                    import os
                    os.makedirs("data", exist_ok=True)
                    loaded = calibrator.load(_CALIBRATOR_CACHE_PATH)
                    logger.info("Calibrator: loaded %d cache entries from disk", loaded)
                except Exception:
                    logger.debug("Calibrator: warm-start load failed (non-fatal)", exc_info=True)
                calibrator.start_background_refresh()

            current_step = "position_manager"
            current_started = time.monotonic()
            container.position_manager.start(
                reconcile_interval=signal_config_loader().position_reconcile_interval
            )
            try:
                recovery_result = container.position_manager.sync_open_positions()
                logger.info("PositionManager startup sync: %s", recovery_result)
            except Exception:
                logger.warning(
                    "PositionManager startup sync failed",
                    exc_info=True,
                )
            mark_startup_step("position_manager", "ready", current_started)

            current_step = "monitoring"
            current_started = time.monotonic()
            container.monitoring_manager.register_component(
                "data_ingestion",
                container.ingestor,
                ["queue_stats"],
            )
            container.monitoring_manager.register_component(
                "indicator_calculation",
                container.indicator_manager,
                ["indicator_freshness", "cache_stats", "performance_stats"],
            )
            container.monitoring_manager.register_component(
                "market_data",
                container.service,
                ["data_latency"],
            )
            container.monitoring_manager.register_component(
                "economic_calendar",
                container.economic_calendar_service,
                ["economic_calendar"],
            )
            container.monitoring_manager.register_component(
                "signals",
                container.signal_runtime,
                ["status"],
            )
            container.monitoring_manager.start()
            mark_startup_step("monitoring", "ready", current_started)
            record_runtime_task_status(
                "startup",
                "monitoring",
                "ready",
                startup_status["steps"]["monitoring"]["duration_ms"],
            )

            container.health_monitor.record_metric(
                "system",
                "startup",
                1.0,
                {"version": "unified", "timestamp": "now"},
            )
            startup_status["phase"] = "running"
            startup_status["ready"] = True
            startup_status["completed_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            startup_status["duration_ms"] = sum(
                step.get("duration_ms", 0)
                for step in startup_status["steps"].values()
            )
        except Exception as exc:  # pragma: no cover
            mark_startup_step(current_step, "failed", current_started, error=str(exc))
            record_runtime_task_status(
                "startup",
                current_step,
                "failed",
                startup_status["steps"][current_step]["duration_ms"],
                error=str(exc),
            )
            startup_status["phase"] = "failed"
            startup_status["ready"] = False
            startup_status["last_error"] = str(exc)
            logger.exception("Failed to start unified services: %s", exc)
            shutdown_components(container)
            raise

        try:
            yield
        finally:
            startup_status["phase"] = "stopping"
            shutdown_components(container)
            if container.health_monitor:
                container.health_monitor.record_metric(
                    "system",
                    "shutdown",
                    1.0,
                    {"timestamp": "now"},
                )
            startup_status["phase"] = "stopped"
            startup_status["ready"] = False

    return lifespan
