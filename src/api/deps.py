"""Unified dependency container."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import threading
import time
from typing import Optional

from src.api.factories import (
    build_signal_components,
    build_trading_components,
    create_indicator_manager,
    create_ingestor,
    create_market_service,
    create_storage_writer,
    register_signal_hot_reload,
)
from src.api.lifespan import create_lifespan, shutdown_components
from src.config import (
    get_effective_config_snapshot,
    get_economic_config,
    get_risk_config,
    get_runtime_ingest_settings,
    get_runtime_market_settings,
    get_signal_config,
    load_db_settings,
    load_mt5_settings,
    load_storage_settings,
)
from src.calendar import EconomicCalendarService
from src.market import MarketDataService
from src.indicators.manager import UnifiedIndicatorManager
from src.ingestion.ingestor import BackgroundIngestor
from src.market_structure import MarketStructureAnalyzer
from src.monitoring import get_health_monitor, get_monitoring_manager
from src.persistence.storage_writer import StorageWriter
from src.risk.service import PreTradeRiskService
from src.signals.evaluation.calibrator import ConfidenceCalibrator
from src.signals.evaluation.performance import StrategyPerformanceTracker
from src.signals.orchestration import SignalRuntime
from src.signals.service import SignalModule
from src.signals.strategies.htf_cache import HTFStateCache
from src.trading import TradingAccountRegistry, TradingModule
from src.trading.signal_quality_tracker import SignalQualityTracker
from src.trading.trade_outcome_tracker import TradeOutcomeTracker
from src.trading.position_manager import PositionManager
from src.trading.pending_entry import PendingEntryManager
from src.trading.signal_executor import TradeExecutor

logger = logging.getLogger(__name__)
_init_lock = threading.Lock()


class _MarketContainer:
    """数据采集与行情缓存域。"""
    service: Optional[MarketDataService] = None
    storage_writer: Optional[StorageWriter] = None
    ingestor: Optional[BackgroundIngestor] = None
    indicator_manager: Optional[UnifiedIndicatorManager] = None


class _SignalContainer:
    """信号评估与置信度域。"""
    signal_module: Optional[SignalModule] = None
    signal_runtime: Optional[SignalRuntime] = None
    htf_cache: Optional[HTFStateCache] = None
    signal_quality_tracker: Optional[SignalQualityTracker] = None
    calibrator: Optional[ConfidenceCalibrator] = None
    performance_tracker: Optional[StrategyPerformanceTracker] = None
    market_structure_analyzer: Optional[MarketStructureAnalyzer] = None


class _TradingContainer:
    """交易执行与持仓域。"""
    trade_registry: Optional[TradingAccountRegistry] = None
    trade_module: Optional[TradingModule] = None
    trade_executor: Optional[TradeExecutor] = None
    position_manager: Optional[PositionManager] = None
    trade_outcome_tracker: Optional[TradeOutcomeTracker] = None
    pending_entry_manager: Optional[PendingEntryManager] = None


class _MonitoringContainer:
    """监控与健康检查域。"""
    health_monitor: Optional[object] = None
    monitoring_manager: Optional[object] = None


class _Container:
    """聚合容器 — 按功能域拆分的子容器 + 跨域组件。"""

    def __init__(self) -> None:
        self.market = _MarketContainer()
        self.signal = _SignalContainer()
        self.trading = _TradingContainer()
        self.monitoring = _MonitoringContainer()
        self.economic_calendar_service: Optional[EconomicCalendarService] = None
        self.market_impact_analyzer: Optional[Any] = None

    # 向后兼容属性 — shutdown_components / lifespan / Depends 函数通过这些属性访问
    @property
    def service(self) -> Optional[MarketDataService]:
        return self.market.service

    @service.setter
    def service(self, v: Optional[MarketDataService]) -> None:
        self.market.service = v

    @property
    def storage_writer(self) -> Optional[StorageWriter]:
        return self.market.storage_writer

    @storage_writer.setter
    def storage_writer(self, v: Optional[StorageWriter]) -> None:
        self.market.storage_writer = v

    @property
    def ingestor(self) -> Optional[BackgroundIngestor]:
        return self.market.ingestor

    @ingestor.setter
    def ingestor(self, v: Optional[BackgroundIngestor]) -> None:
        self.market.ingestor = v

    @property
    def indicator_manager(self) -> Optional[UnifiedIndicatorManager]:
        return self.market.indicator_manager

    @indicator_manager.setter
    def indicator_manager(self, v: Optional[UnifiedIndicatorManager]) -> None:
        self.market.indicator_manager = v

    @property
    def signal_module(self) -> Optional[SignalModule]:
        return self.signal.signal_module

    @signal_module.setter
    def signal_module(self, v: Optional[SignalModule]) -> None:
        self.signal.signal_module = v

    @property
    def signal_runtime(self) -> Optional[SignalRuntime]:
        return self.signal.signal_runtime

    @signal_runtime.setter
    def signal_runtime(self, v: Optional[SignalRuntime]) -> None:
        self.signal.signal_runtime = v

    @property
    def htf_cache(self) -> Optional[HTFStateCache]:
        return self.signal.htf_cache

    @htf_cache.setter
    def htf_cache(self, v: Optional[HTFStateCache]) -> None:
        self.signal.htf_cache = v

    @property
    def signal_quality_tracker(self) -> Optional[SignalQualityTracker]:
        return self.signal.signal_quality_tracker

    @signal_quality_tracker.setter
    def signal_quality_tracker(self, v: Optional[SignalQualityTracker]) -> None:
        self.signal.signal_quality_tracker = v

    @property
    def calibrator(self) -> Optional[ConfidenceCalibrator]:
        return self.signal.calibrator

    @calibrator.setter
    def calibrator(self, v: Optional[ConfidenceCalibrator]) -> None:
        self.signal.calibrator = v

    @property
    def performance_tracker(self) -> Optional[StrategyPerformanceTracker]:
        return self.signal.performance_tracker

    @performance_tracker.setter
    def performance_tracker(self, v: Optional[StrategyPerformanceTracker]) -> None:
        self.signal.performance_tracker = v

    @property
    def market_structure_analyzer(self) -> Optional[MarketStructureAnalyzer]:
        return self.signal.market_structure_analyzer

    @market_structure_analyzer.setter
    def market_structure_analyzer(self, v: Optional[MarketStructureAnalyzer]) -> None:
        self.signal.market_structure_analyzer = v

    @property
    def trade_registry(self) -> Optional[TradingAccountRegistry]:
        return self.trading.trade_registry

    @trade_registry.setter
    def trade_registry(self, v: Optional[TradingAccountRegistry]) -> None:
        self.trading.trade_registry = v

    @property
    def trade_module(self) -> Optional[TradingModule]:
        return self.trading.trade_module

    @trade_module.setter
    def trade_module(self, v: Optional[TradingModule]) -> None:
        self.trading.trade_module = v

    @property
    def trade_executor(self) -> Optional[TradeExecutor]:
        return self.trading.trade_executor

    @trade_executor.setter
    def trade_executor(self, v: Optional[TradeExecutor]) -> None:
        self.trading.trade_executor = v

    @property
    def position_manager(self) -> Optional[PositionManager]:
        return self.trading.position_manager

    @position_manager.setter
    def position_manager(self, v: Optional[PositionManager]) -> None:
        self.trading.position_manager = v

    @property
    def trade_outcome_tracker(self) -> Optional[TradeOutcomeTracker]:
        return self.trading.trade_outcome_tracker

    @trade_outcome_tracker.setter
    def trade_outcome_tracker(self, v: Optional[TradeOutcomeTracker]) -> None:
        self.trading.trade_outcome_tracker = v

    @property
    def pending_entry_manager(self) -> Optional[PendingEntryManager]:
        return self.trading.pending_entry_manager

    @pending_entry_manager.setter
    def pending_entry_manager(self, v: Optional[PendingEntryManager]) -> None:
        self.trading.pending_entry_manager = v

    @property
    def health_monitor(self) -> Optional[object]:
        return self.monitoring.health_monitor

    @health_monitor.setter
    def health_monitor(self, v: Optional[object]) -> None:
        self.monitoring.health_monitor = v

    @property
    def monitoring_manager(self) -> Optional[object]:
        return self.monitoring.monitoring_manager

    @monitoring_manager.setter
    def monitoring_manager(self, v: Optional[object]) -> None:
        self.monitoring.monitoring_manager = v


_c = _Container()
_initialized = False

_startup_status = {
    "phase": "not_started",
    "started_at": None,
    "completed_at": None,
    "duration_ms": None,
    "ready": False,
    "last_error": None,
    "steps": {},
}


def _enum_or_raw(value) -> str:
    return getattr(value, "value", value)


def _reset_startup_status() -> None:
    _startup_status["phase"] = "initializing"
    _startup_status["started_at"] = None
    _startup_status["completed_at"] = None
    _startup_status["duration_ms"] = None
    _startup_status["ready"] = False
    _startup_status["last_error"] = None
    _startup_status["steps"] = {}


def _shutdown_components() -> None:
    shutdown_components(_c)


def _mark_startup_step(
    name: str, state: str, started_at: float, error: Optional[str] = None
) -> None:
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


def get_runtime_task_status(
    component: Optional[str] = None, task_name: Optional[str] = None
) -> list[dict]:
    _ensure_initialized()
    assert _c.storage_writer is not None
    rows = _c.storage_writer.db.fetch_runtime_task_status(
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


def _record_runtime_task_status(
    component: str,
    task_name: str,
    state: str,
    duration_ms: int,
    error: Optional[str] = None,
) -> None:
    if _c.storage_writer is None:
        return
    success_count = 1 if state == "ready" else 0
    failure_count = 1 if state == "failed" else 0
    try:
        _c.storage_writer.db.write_runtime_task_status(
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
        logger.debug(
            "Failed to persist startup runtime task status for %s.%s",
            component,
            task_name,
            exc_info=True,
        )


def _ensure_initialized() -> None:
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return

        ingest_settings = get_runtime_ingest_settings()
        market_settings = get_runtime_market_settings()

        _c.service = create_market_service(load_mt5_settings(), market_settings)
        _c.storage_writer = create_storage_writer(
            load_db_settings(),
            load_storage_settings(),
        )
        _c.service.attach_storage(_c.storage_writer)
        _c.ingestor = create_ingestor(_c.service, _c.storage_writer, ingest_settings)
        _c.indicator_manager = create_indicator_manager(_c.service, _c.storage_writer)

        trading_components = build_trading_components(
            _c.storage_writer,
            get_economic_config(),
        )
        _c.economic_calendar_service = trading_components.economic_calendar_service
        _c.trade_registry = trading_components.trade_registry
        _c.trade_module = trading_components.trade_module

        # Market Impact Analyzer
        economic_settings = get_economic_config()
        if economic_settings.market_impact_enabled:
            from src.calendar.economic_calendar.market_impact import MarketImpactAnalyzer

            _c.market_impact_analyzer = MarketImpactAnalyzer(
                db_writer=_c.storage_writer.db,
                market_repo=_c.storage_writer.db.market_repo,
                settings=economic_settings,
            )
            _c.economic_calendar_service.market_impact_analyzer = _c.market_impact_analyzer

        signal_components = build_signal_components(
            indicator_manager=_c.indicator_manager,
            storage_writer=_c.storage_writer,
            trade_module=_c.trade_module,
            economic_calendar_service=_c.economic_calendar_service,
            signal_config=get_signal_config(),
        )
        _c.calibrator = signal_components.calibrator
        _c.market_structure_analyzer = signal_components.market_structure_analyzer
        _c.signal_module = signal_components.signal_module
        _c.signal_runtime = signal_components.signal_runtime
        _c.htf_cache = signal_components.htf_cache
        _c.signal_quality_tracker = signal_components.signal_quality_tracker
        _c.trade_outcome_tracker = signal_components.trade_outcome_tracker
        _c.position_manager = signal_components.position_manager
        _c.trade_executor = signal_components.trade_executor
        _c.performance_tracker = signal_components.performance_tracker
        _c.pending_entry_manager = signal_components.pending_entry_manager
        register_signal_hot_reload(
            _c.signal_runtime,
            get_signal_config,
            trade_executor=_c.trade_executor,
            economic_calendar_service=_c.economic_calendar_service,
            market_structure_analyzer=_c.market_structure_analyzer,
            performance_tracker=_c.performance_tracker,
            pending_entry_manager=_c.pending_entry_manager,
        )

        _c.health_monitor = get_health_monitor("health_monitor.db")
        _c.health_monitor.configure_alerts(
            data_latency_warning=max(1.0, ingest_settings.max_allowed_delay / 2.0),
            data_latency_critical=max(1.0, ingest_settings.max_allowed_delay),
        )
        economic_settings = get_economic_config()
        _c.health_monitor.alerts["economic_calendar_staleness"] = {
            "warning": max(1.0, economic_settings.stale_after_seconds / 2.0),
            "critical": max(1.0, economic_settings.stale_after_seconds),
        }
        _c.health_monitor.alerts["economic_provider_failures"] = {
            "warning": 1.0,
            "critical": float(max(2, economic_settings.request_retries)),
        }
        monitoring_interval = max(
            1,
            int(
                min(
                    ingest_settings.health_check_interval,
                    ingest_settings.queue_monitor_interval,
                )
            ),
        )
        _c.monitoring_manager = get_monitoring_manager(
            _c.health_monitor,
            check_interval=monitoring_interval,
        )
        _c.health_monitor.cleanup_old_data(days_to_keep=30)
        _c.indicator_manager.cleanup_old_events(days_to_keep=7)

        logger.info(
            "Effective runtime config: %s",
            json.dumps(
                {
                    **get_effective_config_snapshot(),
                    "risk": get_risk_config().model_dump(),
                    "active_trading_account": trading_components.default_account_alias,
                    "trading_account": (
                        _c.trade_module.list_accounts()[0]
                        if _c.trade_module
                        else None
                    ),
                    "indicator_scope": {
                        "symbols": list(_c.indicator_manager.config.symbols),
                        "timeframes": list(_c.indicator_manager.config.timeframes),
                        "inherit_symbols": _c.indicator_manager.config.inherit_symbols,
                        "inherit_timeframes": _c.indicator_manager.config.inherit_timeframes,
                        "indicator_reload_interval": _c.indicator_manager.config.reload_interval,
                        "indicator_poll_interval": _c.indicator_manager.config.pipeline.poll_interval,
                        "indicator_cache_maxsize": _c.indicator_manager.config.pipeline.cache_maxsize,
                        "indicator_cache_strategy": _enum_or_raw(
                            _c.indicator_manager.config.pipeline.cache_strategy
                        ),
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        # 仅在所有组件初始化成功后标记为已初始化，
        # 防止部分初始化后后续调用因 _initialized=True 跳过重初始化
        _initialized = True


def get_runtime_mode() -> str:
    return "unified"


def is_monitoring_enabled() -> bool:
    return True


def get_market_service() -> MarketDataService:
    _ensure_initialized()
    assert _c.service is not None
    return _c.service


def get_account_service() -> TradingModule:
    _ensure_initialized()
    assert _c.trade_module is not None
    return _c.trade_module


def get_trading_service() -> TradingModule:
    _ensure_initialized()
    assert _c.trade_module is not None
    return _c.trade_module


def get_pre_trade_risk_service() -> PreTradeRiskService:
    _ensure_initialized()
    assert _c.trade_registry is not None and _c.trade_module is not None
    trading_service = _c.trade_registry.get_trading_service(
        _c.trade_module.active_account_alias
    )
    return trading_service.pre_trade_risk_service


def get_economic_calendar_service() -> EconomicCalendarService:
    _ensure_initialized()
    assert _c.economic_calendar_service is not None
    return _c.economic_calendar_service


def get_ingestor() -> BackgroundIngestor:
    _ensure_initialized()
    assert _c.ingestor is not None
    return _c.ingestor


def get_indicator_manager() -> UnifiedIndicatorManager:
    _ensure_initialized()
    assert _c.indicator_manager is not None
    return _c.indicator_manager


def get_indicator_worker() -> UnifiedIndicatorManager:
    return get_indicator_manager()


def get_unified_indicator_manager() -> UnifiedIndicatorManager:
    return get_indicator_manager()


def get_signal_service() -> SignalModule:
    _ensure_initialized()
    assert _c.signal_module is not None
    return _c.signal_module


def get_signal_runtime() -> SignalRuntime:
    _ensure_initialized()
    assert _c.signal_runtime is not None
    return _c.signal_runtime


def get_market_structure_analyzer() -> MarketStructureAnalyzer:
    _ensure_initialized()
    assert _c.market_structure_analyzer is not None
    return _c.market_structure_analyzer


def get_trade_executor() -> TradeExecutor:
    _ensure_initialized()
    assert _c.trade_executor is not None
    return _c.trade_executor


def get_position_manager() -> PositionManager:
    _ensure_initialized()
    assert _c.position_manager is not None
    return _c.position_manager


def get_calibrator() -> ConfidenceCalibrator:
    _ensure_initialized()
    assert _c.calibrator is not None
    return _c.calibrator


def get_htf_cache() -> HTFStateCache:
    _ensure_initialized()
    assert _c.htf_cache is not None
    return _c.htf_cache


def get_signal_quality_tracker() -> SignalQualityTracker:
    _ensure_initialized()
    assert _c.signal_quality_tracker is not None
    return _c.signal_quality_tracker


def get_trade_outcome_tracker() -> TradeOutcomeTracker:
    _ensure_initialized()
    assert _c.trade_outcome_tracker is not None
    return _c.trade_outcome_tracker


def get_pending_entry_manager() -> PendingEntryManager:
    _ensure_initialized()
    assert _c.pending_entry_manager is not None
    return _c.pending_entry_manager


def get_health_monitor_instance():
    _ensure_initialized()
    return _c.health_monitor


def get_monitoring_manager_instance():
    _ensure_initialized()
    return _c.monitoring_manager


lifespan = create_lifespan(
    container=_c,
    startup_status=_startup_status,
    ensure_initialized=_ensure_initialized,
    reset_startup_status=_reset_startup_status,
    mark_startup_step=_mark_startup_step,
    record_runtime_task_status=_record_runtime_task_status,
    signal_config_loader=get_signal_config,
)
