"""Unified dependency container — thin FastAPI DI adapter.

All component construction is delegated to :mod:`src.app_runtime`.
This module only provides getter functions for ``FastAPI.Depends()``.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from src.app_runtime.builder import build_app_container
from src.app_runtime.container import AppContainer
from src.app_runtime.runtime import AppRuntime
from src.calendar import EconomicCalendarService
from src.config import get_signal_config
from src.indicators.manager import UnifiedIndicatorManager
from src.ingestion.ingestor import BackgroundIngestor
from src.market import MarketDataService
from src.market_structure import MarketStructureAnalyzer
from src.persistence.storage_writer import StorageWriter
from src.risk.service import PreTradeRiskService
from src.signals.evaluation.calibrator import ConfidenceCalibrator
from src.signals.evaluation.performance import StrategyPerformanceTracker
from src.signals.orchestration import SignalRuntime
from src.signals.service import SignalModule
from src.signals.strategies.htf_cache import HTFStateCache
from src.trading import TradingAccountRegistry, TradingModule
from src.trading.pending_entry import PendingEntryManager
from src.trading.position_manager import PositionManager
from src.trading.signal_executor import TradeExecutor
from src.trading.signal_quality_tracker import SignalQualityTracker
from src.monitoring.pipeline_event_bus import PipelineEventBus
from src.readmodels.runtime import RuntimeReadModel
from src.studio.service import StudioService
from src.trading.trade_outcome_tracker import TradeOutcomeTracker

logger = logging.getLogger(__name__)
_init_lock = threading.Lock()

# ── Global singleton ──────────────────────────────────────────
_runtime: Optional[AppRuntime] = None
_container: Optional[AppContainer] = None
_init_failed: bool = False
_init_error: Optional[Exception] = None


def _ensure_initialized() -> None:
    global _runtime, _container, _init_failed, _init_error
    if _container is not None:
        return
    if _init_failed:
        raise RuntimeError(
            f"Container initialization previously failed: {_init_error}"
        )
    with _init_lock:
        if _container is not None:
            return
        if _init_failed:
            raise RuntimeError(
                f"Container initialization previously failed: {_init_error}"
            )
        try:
            _container = build_app_container(signal_config_loader=get_signal_config)
            _runtime = AppRuntime(_container, signal_config_loader=get_signal_config)
        except Exception as exc:
            _init_failed = True
            _init_error = exc
            logger.exception("Container initialization failed permanently")
            raise


def _shutdown_initialized_runtime() -> None:
    global _runtime, _container
    runtime = _runtime
    _runtime = None
    _container = None
    if runtime is None:
        return
    try:
        runtime.stop()
    except Exception:
        logger.exception("Failed to stop runtime during lifespan shutdown")


# ── Startup status ────────────────────────────────────────────


def get_startup_status() -> dict:
    if _runtime is None:
        return {
            "phase": "not_started",
            "started_at": None,
            "completed_at": None,
            "duration_ms": None,
            "ready": False,
            "last_error": None,
            "steps": {},
        }
    return _runtime.status


def get_runtime_task_status(
    component: Optional[str] = None, task_name: Optional[str] = None
) -> list[dict]:
    _ensure_initialized()
    assert _container is not None and _container.storage_writer is not None
    rows = _container.storage_writer.db.fetch_runtime_task_status(
        component=component,
        task_name=task_name,
    )
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


# ── Lifespan ──────────────────────────────────────────────────


@asynccontextmanager
async def _lifespan(_app):  # type: ignore[no-untyped-def]
    _ensure_initialized()
    assert _runtime is not None
    _runtime.start()
    try:
        yield
    finally:
        _shutdown_initialized_runtime()


lifespan = _lifespan


# ── DI getters ────────────────────────────────────────────────


def get_runtime_mode() -> str:
    return "unified"


def is_monitoring_enabled() -> bool:
    return True


def get_market_service() -> MarketDataService:
    _ensure_initialized()
    assert _container is not None and _container.market_service is not None
    return _container.market_service


def get_account_service() -> TradingModule:
    _ensure_initialized()
    assert _container is not None and _container.trade_module is not None
    return _container.trade_module


def get_trading_service() -> TradingModule:
    _ensure_initialized()
    assert _container is not None and _container.trade_module is not None
    return _container.trade_module


def get_pre_trade_risk_service() -> PreTradeRiskService:
    _ensure_initialized()
    assert _container is not None
    assert _container.trade_registry is not None and _container.trade_module is not None
    trading_service = _container.trade_registry.get_trading_service(
        _container.trade_module.active_account_alias
    )
    return trading_service.pre_trade_risk_service


def get_economic_calendar_service() -> EconomicCalendarService:
    _ensure_initialized()
    assert _container is not None and _container.economic_calendar_service is not None
    return _container.economic_calendar_service


def get_ingestor() -> BackgroundIngestor:
    _ensure_initialized()
    assert _container is not None and _container.ingestor is not None
    return _container.ingestor


def get_indicator_manager() -> UnifiedIndicatorManager:
    _ensure_initialized()
    assert _container is not None and _container.indicator_manager is not None
    return _container.indicator_manager


def get_indicator_worker() -> UnifiedIndicatorManager:
    return get_indicator_manager()


def get_unified_indicator_manager() -> UnifiedIndicatorManager:
    return get_indicator_manager()


def get_signal_service() -> SignalModule:
    _ensure_initialized()
    assert _container is not None and _container.signal_module is not None
    return _container.signal_module


def get_signal_runtime() -> SignalRuntime:
    _ensure_initialized()
    assert _container is not None and _container.signal_runtime is not None
    return _container.signal_runtime


def get_market_structure_analyzer() -> MarketStructureAnalyzer:
    _ensure_initialized()
    assert _container is not None and _container.market_structure_analyzer is not None
    return _container.market_structure_analyzer


def get_trade_executor() -> TradeExecutor:
    _ensure_initialized()
    assert _container is not None and _container.trade_executor is not None
    return _container.trade_executor


def get_position_manager() -> PositionManager:
    _ensure_initialized()
    assert _container is not None and _container.position_manager is not None
    return _container.position_manager


def get_calibrator() -> ConfidenceCalibrator:
    _ensure_initialized()
    assert _container is not None and _container.calibrator is not None
    return _container.calibrator


def get_htf_cache() -> HTFStateCache:
    _ensure_initialized()
    assert _container is not None and _container.htf_cache is not None
    return _container.htf_cache


def get_signal_quality_tracker() -> SignalQualityTracker:
    _ensure_initialized()
    assert _container is not None and _container.signal_quality_tracker is not None
    return _container.signal_quality_tracker


def get_trade_outcome_tracker() -> TradeOutcomeTracker:
    _ensure_initialized()
    assert _container is not None and _container.trade_outcome_tracker is not None
    return _container.trade_outcome_tracker


def get_pending_entry_manager() -> PendingEntryManager:
    _ensure_initialized()
    assert _container is not None and _container.pending_entry_manager is not None
    return _container.pending_entry_manager


def get_health_monitor_instance():  # type: ignore[no-untyped-def]
    _ensure_initialized()
    assert _container is not None
    return _container.health_monitor


def get_monitoring_manager_instance():  # type: ignore[no-untyped-def]
    _ensure_initialized()
    assert _container is not None
    return _container.monitoring_manager


def get_performance_tracker() -> StrategyPerformanceTracker:
    _ensure_initialized()
    assert _container is not None and _container.performance_tracker is not None
    return _container.performance_tracker


def get_pipeline_event_bus() -> PipelineEventBus:
    _ensure_initialized()
    assert _container is not None and _container.pipeline_event_bus is not None
    return _container.pipeline_event_bus


def get_runtime_read_model() -> RuntimeReadModel:
    _ensure_initialized()
    assert _container is not None and _container.runtime_read_model is not None
    return _container.runtime_read_model


def get_studio_service() -> StudioService:
    _ensure_initialized()
    assert _container is not None and _container.studio_service is not None
    return _container.studio_service
