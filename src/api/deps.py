"""
Unified dependency container.

Single runtime mode with all major capabilities enabled:
- ingestion + storage
- unified indicator manager
- monitoring manager
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import logging
import time
from typing import Optional

from src.clients.mt5_market import MT5MarketClient
from src.config import (
    get_effective_config_snapshot,
    get_economic_config,
    get_runtime_ingest_settings,
    get_runtime_market_settings,
    load_db_settings,
    load_mt5_settings,
    load_storage_settings,
)
from src.core.account_service import AccountService
from src.core.economic_calendar_service import EconomicCalendarService
from src.core.market_service import MarketDataService
from src.core.pretrade_risk_service import PreTradeRiskService
from src.core.trading_service import TradingService
from src.indicators.manager import UnifiedIndicatorManager, get_global_unified_manager
from src.ingestion.ingestor import BackgroundIngestor
from src.monitoring.health_check import get_health_monitor, get_monitoring_manager
from src.persistence.db import TimescaleWriter
from src.persistence.storage_writer import StorageWriter

logger = logging.getLogger(__name__)

# Lazily initialized singletons
_service: Optional[MarketDataService] = None
_storage_writer: Optional[StorageWriter] = None
_ingestor: Optional[BackgroundIngestor] = None
_indicator_manager: Optional[UnifiedIndicatorManager] = None
_account_service: Optional[AccountService] = None
_trading_service: Optional[TradingService] = None
_pre_trade_risk_service: Optional[PreTradeRiskService] = None
_economic_calendar_service: Optional[EconomicCalendarService] = None
_health_monitor = None
_monitoring_manager = None
_startup_status = {
    "phase": "not_started",
    "started_at": None,
    "completed_at": None,
    "duration_ms": None,
    "ready": False,
    "last_error": None,
    "steps": {},
}


def _reset_startup_status() -> None:
    _startup_status["phase"] = "initializing"
    _startup_status["started_at"] = None
    _startup_status["completed_at"] = None
    _startup_status["duration_ms"] = None
    _startup_status["ready"] = False
    _startup_status["last_error"] = None
    _startup_status["steps"] = {}


def _mark_startup_step(name: str, state: str, started_at: float, error: Optional[str] = None) -> None:
    duration_ms = int((time.monotonic() - started_at) * 1000)
    _startup_status["steps"][name] = {
        "state": state,
        "duration_ms": duration_ms,
        "error": error,
    }


def get_startup_status() -> dict:
    return {
        "phase": _startup_status["phase"],
        "started_at": _startup_status["started_at"],
        "completed_at": _startup_status["completed_at"],
        "duration_ms": _startup_status["duration_ms"],
        "ready": _startup_status["ready"],
        "last_error": _startup_status["last_error"],
        "steps": dict(_startup_status["steps"]),
    }


def get_runtime_task_status(component: Optional[str] = None, task_name: Optional[str] = None) -> list[dict]:
    _ensure_initialized()
    rows = _storage_writer.db.fetch_runtime_task_status(component=component, task_name=task_name)  # type: ignore[union-attr]
    return [
        {
            "component": row[0],
            "task_name": row[1],
            "updated_at": row[2].isoformat() if row[2] else None,
            "state": row[3],
            "started_at": row[4].isoformat() if row[4] else None,
            "completed_at": row[5].isoformat() if row[5] else None,
            "next_run_at": row[6].isoformat() if row[6] else None,
            "duration_ms": row[7],
            "success_count": int(row[8] or 0),
            "failure_count": int(row[9] or 0),
            "consecutive_failures": int(row[10] or 0),
            "last_error": row[11],
            "details": row[12] or {},
        }
        for row in rows
    ]


def _record_runtime_task_status(component: str, task_name: str, state: str, duration_ms: int, error: Optional[str] = None) -> None:
    if _storage_writer is None:
        return
    success_count = 1 if state == "ready" else 0
    failure_count = 1 if state == "failed" else 0
    try:
        _storage_writer.db.write_runtime_task_status(
            [
                (
                    component,
                    task_name,
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
        logger.debug("Failed to persist startup runtime task status for %s.%s", component, task_name, exc_info=True)


def _ensure_initialized() -> None:
    global _service
    global _storage_writer
    global _ingestor
    global _indicator_manager
    global _account_service
    global _trading_service
    global _pre_trade_risk_service
    global _economic_calendar_service
    global _health_monitor
    global _monitoring_manager

    if _service is not None:
        return

    mt5_settings = load_mt5_settings()
    db_settings = load_db_settings()
    ingest_settings = get_runtime_ingest_settings()
    storage_settings = load_storage_settings()
    market_settings = get_runtime_market_settings()

    mt5_client = MT5MarketClient(mt5_settings)
    _service = MarketDataService(client=mt5_client, market_settings=market_settings)
    _storage_writer = StorageWriter(TimescaleWriter(db_settings), storage_settings=storage_settings)
    _service.attach_storage(_storage_writer)
    _ingestor = BackgroundIngestor(
        service=_service,
        storage=_storage_writer,
        ingest_settings=ingest_settings,
    )
    _indicator_manager = get_global_unified_manager(
        market_service=_service,
        storage_writer=_storage_writer,
        config_file="config/indicators.json",
        start_immediately=False,
    )
    _account_service = AccountService()
    _economic_calendar_service = EconomicCalendarService(
        db_writer=_storage_writer.db,
        settings=get_economic_config(),
        storage_writer=_storage_writer,
    )
    _pre_trade_risk_service = PreTradeRiskService(
        economic_calendar_service=_economic_calendar_service,
        settings=get_economic_config(),
    )
    _trading_service = TradingService(pre_trade_risk_service=_pre_trade_risk_service)
    _health_monitor = get_health_monitor("health_monitor.db")
    _health_monitor.configure_alerts(
        data_latency_warning=max(1.0, ingest_settings.max_allowed_delay / 2.0),
        data_latency_critical=max(1.0, ingest_settings.max_allowed_delay),
    )
    economic_settings = get_economic_config()
    _health_monitor.alerts["economic_calendar_staleness"] = {
        "warning": max(1.0, economic_settings.stale_after_seconds / 2.0),
        "critical": max(1.0, economic_settings.stale_after_seconds),
    }
    _health_monitor.alerts["economic_provider_failures"] = {
        "warning": 1.0,
        "critical": float(max(2, economic_settings.request_retries)),
    }
    monitoring_interval = max(
        1,
        int(min(ingest_settings.health_check_interval, ingest_settings.queue_monitor_interval)),
    )
    _monitoring_manager = get_monitoring_manager(
        _health_monitor,
        check_interval=monitoring_interval,
    )
    logger.info(
        "Effective runtime config: %s",
        json.dumps(
            {
                **get_effective_config_snapshot(),
                "indicator_scope": {
                    "symbols": list(_indicator_manager.config.symbols),
                    "timeframes": list(_indicator_manager.config.timeframes),
                    "inherit_symbols": _indicator_manager.config.inherit_symbols,
                    "inherit_timeframes": _indicator_manager.config.inherit_timeframes,
                    "indicator_reload_interval": _indicator_manager.config.reload_interval,
                    "indicator_poll_interval": _indicator_manager.config.pipeline.poll_interval,
                    "indicator_cache_maxsize": _indicator_manager.config.pipeline.cache_maxsize,
                    "indicator_cache_strategy": _indicator_manager.config.pipeline.cache_strategy.value,
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )


def get_runtime_mode() -> str:
    return "unified"


def is_monitoring_enabled() -> bool:
    return True


def get_market_service() -> MarketDataService:
    _ensure_initialized()
    return _service  # type: ignore[return-value]


def get_account_service() -> AccountService:
    _ensure_initialized()
    return _account_service  # type: ignore[return-value]


def get_trading_service() -> TradingService:
    _ensure_initialized()
    return _trading_service  # type: ignore[return-value]


def get_pre_trade_risk_service() -> PreTradeRiskService:
    _ensure_initialized()
    return _pre_trade_risk_service  # type: ignore[return-value]


def get_economic_calendar_service() -> EconomicCalendarService:
    _ensure_initialized()
    return _economic_calendar_service  # type: ignore[return-value]


def get_ingestor() -> BackgroundIngestor:
    _ensure_initialized()
    return _ingestor  # type: ignore[return-value]


def get_indicator_manager() -> UnifiedIndicatorManager:
    _ensure_initialized()
    return _indicator_manager  # type: ignore[return-value]


def get_indicator_worker() -> UnifiedIndicatorManager:
    """Backward-compatible alias for older monitoring code."""
    return get_indicator_manager()


def get_unified_indicator_manager() -> UnifiedIndicatorManager:
    return get_indicator_manager()


def get_health_monitor_instance():
    _ensure_initialized()
    return _health_monitor


def get_monitoring_manager_instance():
    _ensure_initialized()
    return _monitoring_manager


@asynccontextmanager
async def lifespan(_app):
    _ensure_initialized()
    _reset_startup_status()
    _startup_status["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    current_step = "initialization"
    current_started = time.monotonic()

    try:
        current_step = "storage"
        current_started = time.monotonic()
        _storage_writer.start()  # type: ignore[union-attr]
        _mark_startup_step("storage", "ready", current_started)
        _record_runtime_task_status("startup", "storage", "ready", _startup_status["steps"]["storage"]["duration_ms"])

        current_step = "ingestion"
        current_started = time.monotonic()
        _ingestor.start()  # type: ignore[union-attr]
        _mark_startup_step("ingestion", "ready", current_started)
        _record_runtime_task_status("startup", "ingestion", "ready", _startup_status["steps"]["ingestion"]["duration_ms"])

        current_step = "economic_calendar"
        current_started = time.monotonic()
        _economic_calendar_service.start()  # type: ignore[union-attr]
        _mark_startup_step("economic_calendar", "ready", current_started)
        _record_runtime_task_status("startup", "economic_calendar", "ready", _startup_status["steps"]["economic_calendar"]["duration_ms"])

        current_step = "indicators"
        current_started = time.monotonic()
        _indicator_manager.start()  # type: ignore[union-attr]
        _mark_startup_step("indicators", "ready", current_started)
        _record_runtime_task_status("startup", "indicators", "ready", _startup_status["steps"]["indicators"]["duration_ms"])

        current_step = "monitoring"
        current_started = time.monotonic()
        _monitoring_manager.register_component(  # type: ignore[union-attr]
            "data_ingestion",
            _ingestor,
            ["queue_stats"],
        )
        _monitoring_manager.register_component(  # type: ignore[union-attr]
            "indicator_calculation",
            _indicator_manager,
            ["indicator_freshness", "cache_stats", "performance_stats"],
        )
        _monitoring_manager.register_component(  # type: ignore[union-attr]
            "market_data",
            _service,
            ["data_latency"],
        )
        _monitoring_manager.register_component(  # type: ignore[union-attr]
            "economic_calendar",
            _economic_calendar_service,
            ["economic_calendar"],
        )
        _monitoring_manager.start()  # type: ignore[union-attr]
        _mark_startup_step("monitoring", "ready", current_started)
        _record_runtime_task_status("startup", "monitoring", "ready", _startup_status["steps"]["monitoring"]["duration_ms"])
        _health_monitor.record_metric(  # type: ignore[union-attr]
            "system",
            "startup",
            1.0,
            {"version": "unified", "timestamp": "now"},
        )
        _startup_status["phase"] = "running"
        _startup_status["ready"] = True
        _startup_status["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        startup_started = _startup_status["started_at"]
        if startup_started:
            # Use monotonic aggregate from measured steps when available.
            _startup_status["duration_ms"] = sum(
                step.get("duration_ms", 0) for step in _startup_status["steps"].values()
            )
    except Exception as exc:  # pragma: no cover
        _mark_startup_step(current_step, "failed", current_started, error=str(exc))
        _record_runtime_task_status(
            "startup",
            current_step,
            "failed",
            _startup_status["steps"][current_step]["duration_ms"],
            error=str(exc),
        )
        _startup_status["phase"] = "failed"
        _startup_status["ready"] = False
        _startup_status["last_error"] = str(exc)
        logger.exception("Failed to start unified services: %s", exc)
        raise

    try:
        yield
    finally:
        _startup_status["phase"] = "stopping"
        _monitoring_manager.stop()  # type: ignore[union-attr]
        if _indicator_manager:
            _indicator_manager.shutdown()
        if _economic_calendar_service:
            _economic_calendar_service.stop()
        _ingestor.stop()  # type: ignore[union-attr]
        _storage_writer.stop()  # type: ignore[union-attr]
        _health_monitor.record_metric(  # type: ignore[union-attr]
            "system",
            "shutdown",
            1.0,
            {"timestamp": "now"},
        )
        _startup_status["phase"] = "stopped"
        _startup_status["ready"] = False
