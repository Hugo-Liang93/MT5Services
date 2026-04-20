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
from src.app_runtime.mode_controller import RuntimeModeController
from src.app_runtime.runtime import AppRuntime
from src.backtesting.paper_trading.bridge import PaperTradingBridge
from src.calendar import EconomicCalendarService
from src.config import get_signal_config
from src.indicators.manager import UnifiedIndicatorManager
from src.ingestion.ingestor import BackgroundIngestor
from src.market import MarketDataService
from src.market_structure import MarketStructureAnalyzer
from src.monitoring.pipeline import PipelineEventBus
from src.notifications.module import NotificationModule
from src.persistence.storage_writer import StorageWriter
from src.readmodels.cockpit import CockpitReadModel
from src.readmodels.intel import IntelReadModel
from src.readmodels.lab_impact import LabImpactReadModel
from src.readmodels.runtime import RuntimeReadModel
from src.readmodels.trade_trace import TradingFlowTraceReadModel
from src.readmodels.trades_workbench import TradesWorkbenchReadModel
from src.readmodels.workbench import WorkbenchReadModel
from src.risk.service import PreTradeRiskService
from src.signals.evaluation.calibrator import ConfidenceCalibrator
from src.signals.evaluation.performance import StrategyPerformanceTracker
from src.signals.orchestration.runtime import SignalRuntime
from src.signals.service import SignalModule
from src.signals.strategies.htf_cache import HTFStateCache
from src.studio.service import StudioService
from src.trading.admission import TradeAdmissionService
from src.trading.application.module import TradingModule
from src.trading.application.services import TradingCommandService, TradingQueryService
from src.trading.closeout import ExposureCloseoutController
from src.trading.commands.service import OperatorCommandService
from src.trading.execution.executor import TradeExecutor
from src.trading.pending import PendingEntryManager
from src.trading.positions import PositionManager
from src.trading.runtime.registry import TradingAccountRegistry
from src.trading.tracking import SignalQualityTracker, TradeOutcomeTracker

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
        raise RuntimeError(f"Container initialization previously failed: {_init_error}")
    with _init_lock:
        if _container is not None:  # double-check under lock (thread safety)
            return  # type: ignore[unreachable]
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


def resolve_runtime_task_scope(
    *,
    instance_id: Optional[str] = None,
    instance_role: Optional[str] = None,
    account_key: Optional[str] = None,
    account_alias: Optional[str] = None,
) -> dict[str, Optional[str]]:
    _ensure_initialized()
    resolved = {
        "instance_id": instance_id,
        "instance_role": instance_role,
        "account_key": account_key,
        "account_alias": account_alias,
    }
    if any(value is not None for value in resolved.values()):
        return resolved
    runtime_identity = getattr(_container, "runtime_identity", None)
    if runtime_identity is None:
        return resolved
    resolved["instance_id"] = runtime_identity.instance_id
    return resolved


def get_runtime_task_status(
    component: Optional[str] = None,
    task_name: Optional[str] = None,
    instance_id: Optional[str] = None,
    instance_role: Optional[str] = None,
    account_key: Optional[str] = None,
    account_alias: Optional[str] = None,
) -> list[dict]:
    _ensure_initialized()
    assert _container is not None and _container.storage_writer is not None
    scope = resolve_runtime_task_scope(
        instance_id=instance_id,
        instance_role=instance_role,
        account_key=account_key,
        account_alias=account_alias,
    )
    rows = _container.storage_writer.db.fetch_runtime_task_status_records(
        component=component,
        task_name=task_name,
        instance_id=scope["instance_id"],
        instance_role=scope["instance_role"],
        account_key=scope["account_key"],
        account_alias=scope["account_alias"],
    )
    return [
        {
            "component": row["component"],
            "task_name": row["task_name"],
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            "state": row["state"],
            "started_at": row["started_at"].isoformat() if row["started_at"] else None,
            "completed_at": (
                row["completed_at"].isoformat() if row["completed_at"] else None
            ),
            "next_run_at": (
                row["next_run_at"].isoformat() if row["next_run_at"] else None
            ),
            "duration_ms": row["duration_ms"],
            "success_count": int(row["success_count"] or 0),
            "failure_count": int(row["failure_count"] or 0),
            "consecutive_failures": int(row["consecutive_failures"] or 0),
            "last_error": row["last_error"],
            "details": row["details"] or {},
            "instance_id": row["instance_id"],
            "instance_role": row["instance_role"],
            "account_key": row["account_key"],
            "account_alias": row["account_alias"],
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
    _ensure_initialized()
    if _container is None or _container.runtime_mode_controller is None:
        return "full"
    snap = _container.runtime_mode_controller.snapshot()
    mode = snap.get("current_mode")
    return str(mode) if mode else "full"


def is_monitoring_enabled() -> bool:
    return True


def get_market_service() -> MarketDataService:
    _ensure_initialized()
    assert _container is not None and _container.market_service is not None
    return _container.market_service


def get_account_service() -> TradingQueryService:
    _ensure_initialized()
    assert _container is not None and _container.trade_module is not None
    return _container.trade_module.queries


def get_trading_service() -> TradingModule:
    _ensure_initialized()
    assert _container is not None and _container.trade_module is not None
    return _container.trade_module


def get_trading_command_service() -> TradingCommandService:
    _ensure_initialized()
    assert _container is not None and _container.trade_module is not None
    return _container.trade_module.commands


def get_trading_query_service() -> TradingQueryService:
    _ensure_initialized()
    assert _container is not None and _container.trade_module is not None
    return _container.trade_module.queries


def get_operator_command_service() -> OperatorCommandService:
    _ensure_initialized()
    assert _container is not None and _container.operator_command_service is not None
    return _container.operator_command_service


def get_notification_module() -> Optional[NotificationModule]:
    """Return the NotificationModule or ``None`` if notifications are disabled.

    Unlike most deps getters, this one returns ``Optional`` because the
    module may legitimately be absent (no bot_token configured). Callers
    must handle ``None`` and return a friendly 503/disabled response.
    """
    _ensure_initialized()
    if _container is None:
        return None
    return _container.notification_module


def get_trade_admission_service() -> TradeAdmissionService:
    _ensure_initialized()
    assert _container is not None and _container.trade_module is not None
    assert _container.runtime_read_model is not None
    return TradeAdmissionService(
        command_service=_container.trade_module.commands,
        runtime_views=_container.runtime_read_model,
        pipeline_event_bus=_container.pipeline_event_bus,
    )


def get_pre_trade_risk_service() -> PreTradeRiskService:
    _ensure_initialized()
    assert _container is not None
    assert _container.trade_registry is not None and _container.trade_module is not None
    trading_service = _container.trade_registry.get_trading_service(
        _container.trade_module.active_account_alias
    )
    assert trading_service.pre_trade_risk_service is not None
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


def get_exposure_closeout_controller() -> ExposureCloseoutController:
    _ensure_initialized()
    assert (
        _container is not None and _container.exposure_closeout_controller is not None
    )
    return _container.exposure_closeout_controller


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


def get_optional_signal_quality_tracker() -> SignalQualityTracker | None:
    _ensure_initialized()
    assert _container is not None
    return _container.signal_quality_tracker


def get_trade_outcome_tracker() -> TradeOutcomeTracker:
    _ensure_initialized()
    assert _container is not None and _container.trade_outcome_tracker is not None
    return _container.trade_outcome_tracker


def get_optional_trade_outcome_tracker() -> TradeOutcomeTracker | None:
    _ensure_initialized()
    assert _container is not None
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


def get_workbench_read_model() -> WorkbenchReadModel:
    """单账户执行工作台聚合读模型（P9 Phase 1）。

    纯组合层，每请求构造一个轻量 WorkbenchReadModel 实例。
    实际数据源仍走 RuntimeReadModel + MarketDataService 单例。
    """
    _ensure_initialized()
    assert _container is not None and _container.runtime_read_model is not None
    return WorkbenchReadModel(
        runtime_read_model=_container.runtime_read_model,
        market_service=_container.market_service,
        runtime_identity=_container.runtime_identity,
    )


def get_trade_trace_read_model() -> TradingFlowTraceReadModel:
    _ensure_initialized()
    assert _container is not None and _container.trade_trace_read_model is not None
    return _container.trade_trace_read_model


def get_cockpit_read_model() -> CockpitReadModel:
    """P10.1：Cockpit 跨账户总控台读模型。

    每请求构造一个新实例；底层 repo / signal module 均为单例。
    数据源跨账户 → 前提：同 environment 的所有实例共享 db.live 或 db.demo。

    P1.3: 注入 IntelReadModel 供 opportunity_queue 块委托调用，消除重复查询。
    """
    _ensure_initialized()
    assert _container is not None
    assert _container.storage_writer is not None
    assert _container.signal_module is not None
    assert _container.runtime_read_model is not None
    intel = IntelReadModel(signal_module=_container.signal_module)
    return CockpitReadModel(
        trading_state_repo=_container.storage_writer.db.trading_state_repo,
        signal_module=_container.signal_module,
        runtime_read_model=_container.runtime_read_model,
        intel_read_model=intel,
    )


def get_lab_impact_read_model() -> LabImpactReadModel:
    """P10.5: Lab Impact 贯通读模型（每请求构造）。

    - backtest_runtime_store 为模块级全局单例（WF / recommendations 内存缓存）
    - paper_trading_repo 走 storage_writer.db
    - backtest_repo 走 api.backtest_routes.execution.get_backtest_repo()（现有工厂，
      模块级缓存的 BacktestRepository 实例，专用连接池，ensure_schema 已执行）
    """
    from src.api.backtest_routes.execution import get_backtest_repo
    from src.backtesting.data import backtest_runtime_store

    _ensure_initialized()
    assert _container is not None
    paper_trading_repo = None
    if _container.storage_writer is not None:
        db = _container.storage_writer.db
        paper_trading_repo = getattr(db, "paper_trading_repo", None)
    return LabImpactReadModel(
        backtest_store=backtest_runtime_store,
        paper_trading_repo=paper_trading_repo,
        backtest_repo=get_backtest_repo(),
    )


def get_intel_read_model() -> IntelReadModel:
    """P10.2: Intel 行动队列读模型（每请求构造）。

    依赖单条：signal_module 提供 recent_signal_page + strategy_account_bindings。
    """
    _ensure_initialized()
    assert _container is not None
    assert _container.signal_module is not None
    return IntelReadModel(signal_module=_container.signal_module)


def get_trades_workbench_read_model() -> TradesWorkbenchReadModel:
    """P10.3：trades/workbench canonical 读模型（每请求构造）。

    signal_repo / trace_read_model 均为单例复用，不持有独立状态。
    """
    _ensure_initialized()
    assert _container is not None
    assert _container.storage_writer is not None
    assert _container.trade_module is not None
    assert _container.trade_trace_read_model is not None
    trade_module = _container.trade_module
    return TradesWorkbenchReadModel(
        signal_repo=_container.storage_writer.db.signal_repo,
        trace_read_model=_container.trade_trace_read_model,
        account_alias_getter=lambda: trade_module.active_account_alias,
    )


def get_runtime_mode_controller() -> RuntimeModeController:
    _ensure_initialized()
    assert _container is not None and _container.runtime_mode_controller is not None
    return _container.runtime_mode_controller


def get_studio_service() -> StudioService:
    _ensure_initialized()
    assert _container is not None and _container.studio_service is not None
    return _container.studio_service


def get_paper_trading_bridge() -> Optional[PaperTradingBridge]:
    _ensure_initialized()
    assert _container is not None
    return _container.paper_trading_bridge
