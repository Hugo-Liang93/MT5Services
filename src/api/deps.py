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
import threading
import time
from typing import Optional

from src.clients.mt5_market import MT5MarketClient
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
from src.config.advanced_manager import get_config_manager
from src.core.economic_calendar_service import EconomicCalendarService
from src.core.market_service import MarketDataService
from src.risk.service import PreTradeRiskService
from src.trading import TradingAccountRegistry, TradingModule
from src.indicators.manager import UnifiedIndicatorManager, get_global_unified_manager
from src.ingestion.ingestor import BackgroundIngestor
from src.monitoring.health_check import get_health_monitor, get_monitoring_manager
from src.persistence.db import TimescaleWriter
from src.persistence.storage_writer import StorageWriter
from src.signals import (
    EconomicEventFilter,
    SessionFilter,
    SignalFilterChain,
    SignalModule,
    SignalPolicy,
    SignalRuntime,
    SignalTarget,
    SpreadFilter,
    TimescaleSignalRepository,
    UnifiedIndicatorSourceAdapter,
)
from src.trading.signal_executor import ExecutorConfig, TradeExecutor
from src.signals.tracking.position_manager import PositionManager
from src.signals.strategies.htf_cache import HTFStateCache
from src.signals.tracking.outcome_tracker import OutcomeTracker
from src.signals.evaluation.calibrator import ConfidenceCalibrator
from src.signals.strategies.registry import register_all_strategies
from src.signals.contracts import normalize_session_name

logger = logging.getLogger(__name__)
_init_lock = threading.Lock()


class _Container:
    """Lazily initialized singleton container for all runtime dependencies."""

    service: Optional[MarketDataService] = None
    storage_writer: Optional[StorageWriter] = None
    ingestor: Optional[BackgroundIngestor] = None
    indicator_manager: Optional[UnifiedIndicatorManager] = None
    trade_registry: Optional[TradingAccountRegistry] = None
    trade_module: Optional[TradingModule] = None
    economic_calendar_service: Optional[EconomicCalendarService] = None
    signal_module: Optional[SignalModule] = None
    signal_runtime: Optional[SignalRuntime] = None
    htf_cache: Optional[HTFStateCache] = None
    outcome_tracker: Optional[OutcomeTracker] = None
    calibrator: Optional[ConfidenceCalibrator] = None
    trade_executor: Optional[TradeExecutor] = None
    position_manager: Optional[PositionManager] = None
    health_monitor: Optional[object] = None
    monitoring_manager: Optional[object] = None


_c = _Container()

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
    """Gracefully stop all background components in reverse startup order."""
    for label, component, method in [
        ("monitoring_manager", _c.monitoring_manager, "stop"),
        ("position_manager", _c.position_manager, "stop"),
        ("signal_runtime", _c.signal_runtime, "stop"),
        ("indicator_manager", _c.indicator_manager, "shutdown"),
        ("economic_calendar_service", _c.economic_calendar_service, "stop"),
        ("ingestor", _c.ingestor, "stop"),
        ("storage_writer", _c.storage_writer, "stop"),
    ]:
        if component is None:
            continue
        try:
            getattr(component, method)()
        except Exception:
            logger.debug("Failed to stop %s during shutdown", label, exc_info=True)


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
        component=component, task_name=task_name
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
    if _c.service is not None:
        return
    with _init_lock:
        if _c.service is not None:
            return

        mt5_settings = load_mt5_settings()
        db_settings = load_db_settings()
        ingest_settings = get_runtime_ingest_settings()
        storage_settings = load_storage_settings()
        market_settings = get_runtime_market_settings()

        mt5_client = MT5MarketClient(mt5_settings)
        _c.service = MarketDataService(
            client=mt5_client, market_settings=market_settings
        )
        _c.storage_writer = StorageWriter(
            TimescaleWriter(db_settings), storage_settings=storage_settings
        )
        _c.service.attach_storage(_c.storage_writer)
        _c.ingestor = BackgroundIngestor(
            service=_c.service,
            storage=_c.storage_writer,
            ingest_settings=ingest_settings,
        )
        _c.indicator_manager = get_global_unified_manager(
            market_service=_c.service,
            storage_writer=_c.storage_writer,
            config_file="config/indicators.json",
            start_immediately=False,
        )
        _c.economic_calendar_service = EconomicCalendarService(
            db_writer=_c.storage_writer.db,
            settings=get_economic_config(),
            storage_writer=_c.storage_writer,
        )
        _c.trade_registry = TradingAccountRegistry(
            economic_calendar_service=_c.economic_calendar_service
        )
        default_alias = _c.trade_registry.default_account_alias()
        _c.trade_module = TradingModule(
            registry=_c.trade_registry,
            db_writer=_c.storage_writer.db,
            active_account_alias=default_alias,
        )
        # ConfidenceCalibrator：alpha=0.3（历史胜率占30%权重），启动时不强制刷新，
        # 首次调用 calibrate() 时自动从 DB 加载（auto_refresh 机制）。
        _c.calibrator = ConfidenceCalibrator(
            fetch_winrates_fn=_c.storage_writer.db.fetch_winrates,
            alpha=0.30,
            baseline_win_rate=0.50,
            max_boost=1.30,
            min_samples=20,
            refresh_interval_seconds=3600,
        )
        _c.signal_module = SignalModule(
            indicator_source=UnifiedIndicatorSourceAdapter(_c.indicator_manager),
            repository=TimescaleSignalRepository(_c.storage_writer.db),
            calibrator=_c.calibrator,
        )
        # ── HTFStateCache + 全量策略注册（必须在 runtime_targets 构建前完成）──────
        # HTFStateCache 自身无外部依赖，可在此时提前创建。
        # register_all_strategies 将复合策略与 MultiTimeframeConfirmStrategy 一并注册，
        # 确保后续 runtime_targets 列表和 SignalRuntime._target_index 包含所有策略名。
        # 若在 SignalRuntime 构建后才注册，MTF 策略将永远不会收到快照事件（已知 bug 的根因）。
        _c.htf_cache = HTFStateCache()
        register_all_strategies(_c.signal_module, _c.htf_cache)
        _c.indicator_manager.set_priority_indicator_groups(
            _c.signal_module.required_indicator_groups()
        )
        runtime_targets = [
            SignalTarget(symbol=symbol, timeframe=timeframe, strategy=strategy)
            for symbol in _c.indicator_manager.config.symbols
            for timeframe in _c.indicator_manager.config.timeframes
            for strategy in _c.signal_module.list_strategies()
        ]
        sig_cfg = get_signal_config()
        allowed_sessions = tuple(
            normalize_session_name(s)
            for s in sig_cfg.allowed_sessions.split(",")
            if s.strip()
        )
        signal_policy = SignalPolicy(
            min_preview_confidence=sig_cfg.min_preview_confidence,
            min_preview_bar_progress=sig_cfg.min_preview_bar_progress,
            min_preview_stable_seconds=sig_cfg.preview_stable_seconds,
            preview_cooldown_seconds=sig_cfg.preview_cooldown_seconds,
            snapshot_dedupe_window_seconds=sig_cfg.snapshot_dedupe_window_seconds,
            max_spread_points=sig_cfg.max_spread_points,
            allowed_sessions=allowed_sessions,
            auto_trade_enabled=sig_cfg.auto_trade_enabled,
            auto_trade_min_confidence=sig_cfg.auto_trade_min_confidence,
            auto_trade_require_armed=sig_cfg.auto_trade_require_armed,
        )
        filter_chain = SignalFilterChain(
            session_filter=SessionFilter(allowed_sessions=allowed_sessions),
            spread_filter=SpreadFilter(max_spread_points=sig_cfg.max_spread_points),
            economic_filter=EconomicEventFilter(
                provider=(
                    _c.economic_calendar_service
                    if sig_cfg.economic_filter_enabled
                    else None
                ),
                lookahead_minutes=sig_cfg.economic_lookahead_minutes,
                lookback_minutes=sig_cfg.economic_lookback_minutes,
                importance_min=sig_cfg.economic_importance_min,
            ),
        )
        _c.signal_runtime = SignalRuntime(
            service=_c.signal_module,
            snapshot_source=_c.indicator_manager,
            targets=runtime_targets,
            enable_confirmed_snapshot=True,
            enable_intrabar=True,
            policy=signal_policy,
            filter_chain=filter_chain,
        )
        executor_config = ExecutorConfig(
            enabled=sig_cfg.auto_trade_enabled,
            min_confidence=sig_cfg.auto_trade_min_confidence,
            require_armed=sig_cfg.auto_trade_require_armed,
            risk_percent=sig_cfg.risk_percent_per_trade,
            sl_atr_multiplier=sig_cfg.sl_atr_multiplier,
            tp_atr_multiplier=sig_cfg.tp_atr_multiplier,
            min_volume=sig_cfg.min_volume,
            max_volume=sig_cfg.max_volume,
        )
        _c.position_manager = PositionManager(
            trading_module=_c.trade_module,
            trailing_atr_multiplier=sig_cfg.trailing_atr_multiplier,
            breakeven_atr_threshold=sig_cfg.breakeven_atr_threshold,
        )
        # T-4: 若 DB 可用，传入持久化回调写入 auto_executions 表
        _persist_exec_fn = None
        if _c.storage_writer is not None and hasattr(_c.storage_writer, "db"):
            db = _c.storage_writer.db
            if hasattr(db, "write_auto_executions"):
                _persist_exec_fn = db.write_auto_executions
        _c.trade_executor = TradeExecutor(
            trading_module=_c.trade_module,
            config=executor_config,
            position_manager=_c.position_manager,
            htf_cache=_c.htf_cache,
            persist_execution_fn=_persist_exec_fn,
        )
    _c.signal_runtime.add_signal_listener(_c.trade_executor.on_signal_event)
    # HTFStateCache 注册为 signal_runtime 的监听器（必须在 signal_runtime 构建后）
    _c.htf_cache.attach(_c.signal_runtime)

    # 配置热加载：signal.ini 变更后自动更新 SignalRuntime.policy
    def _on_signal_config_change(filename: str) -> None:
        if filename != "signal.ini":
            return
        try:
            new_sig_cfg = get_signal_config()
            new_sessions = tuple(
                normalize_session_name(s)
                for s in new_sig_cfg.allowed_sessions.split(",")
                if s.strip()
            )
            new_policy = SignalPolicy(
                min_preview_confidence=new_sig_cfg.min_preview_confidence,
                min_preview_bar_progress=new_sig_cfg.min_preview_bar_progress,
                min_preview_stable_seconds=new_sig_cfg.preview_stable_seconds,
                preview_cooldown_seconds=new_sig_cfg.preview_cooldown_seconds,
                snapshot_dedupe_window_seconds=new_sig_cfg.snapshot_dedupe_window_seconds,
                max_spread_points=new_sig_cfg.max_spread_points,
                allowed_sessions=new_sessions,
                auto_trade_enabled=new_sig_cfg.auto_trade_enabled,
                auto_trade_min_confidence=new_sig_cfg.auto_trade_min_confidence,
                auto_trade_require_armed=new_sig_cfg.auto_trade_require_armed,
            )
            if _c.signal_runtime is not None:
                _c.signal_runtime.policy = new_policy
            logger.info("signal.ini hot-reloaded: policy updated")
        except Exception:
            logger.exception("Failed to hot-reload signal.ini")

    get_config_manager().register_change_callback(_on_signal_config_change)
    # OutcomeTracker：N 根 bar 后回填信号绩效
    _c.outcome_tracker = OutcomeTracker(
        write_fn=_c.storage_writer.db.write_outcome_events,
    )
    _c.outcome_tracker.attach(_c.signal_runtime)
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
                "active_trading_account": default_alias,
                "trading_account": (
                    _c.trade_module.list_accounts()[0] if _c.trade_module else None
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
    """Backward-compatible alias for older monitoring code."""
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


def get_outcome_tracker() -> OutcomeTracker:
    _ensure_initialized()
    assert _c.outcome_tracker is not None
    return _c.outcome_tracker


def get_health_monitor_instance():
    _ensure_initialized()
    return _c.health_monitor


def get_monitoring_manager_instance():
    _ensure_initialized()
    return _c.monitoring_manager


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
        _c.storage_writer.start()
        _mark_startup_step("storage", "ready", current_started)
        _record_runtime_task_status(
            "startup",
            "storage",
            "ready",
            _startup_status["steps"]["storage"]["duration_ms"],
        )

        current_step = "ingestion"
        current_started = time.monotonic()
        _c.ingestor.start()
        _mark_startup_step("ingestion", "ready", current_started)
        _record_runtime_task_status(
            "startup",
            "ingestion",
            "ready",
            _startup_status["steps"]["ingestion"]["duration_ms"],
        )

        current_step = "economic_calendar"
        current_started = time.monotonic()
        _c.economic_calendar_service.start()
        _mark_startup_step("economic_calendar", "ready", current_started)
        _record_runtime_task_status(
            "startup",
            "economic_calendar",
            "ready",
            _startup_status["steps"]["economic_calendar"]["duration_ms"],
        )

        current_step = "indicators"
        current_started = time.monotonic()
        _c.indicator_manager.start()
        _mark_startup_step("indicators", "ready", current_started)
        _record_runtime_task_status(
            "startup",
            "indicators",
            "ready",
            _startup_status["steps"]["indicators"]["duration_ms"],
        )

        current_step = "signals"
        current_started = time.monotonic()
        _c.signal_runtime.start()
        _mark_startup_step("signals", "ready", current_started)
        _record_runtime_task_status(
            "startup",
            "signals",
            "ready",
            _startup_status["steps"]["signals"]["duration_ms"],
        )

        current_step = "position_manager"
        current_started = time.monotonic()
        _c.position_manager.start(
            reconcile_interval=get_signal_config().position_reconcile_interval
        )
        _mark_startup_step("position_manager", "ready", current_started)

        current_step = "monitoring"
        current_started = time.monotonic()
        _c.monitoring_manager.register_component(
            "data_ingestion",
            _c.ingestor,
            ["queue_stats"],
        )
        _c.monitoring_manager.register_component(
            "indicator_calculation",
            _c.indicator_manager,
            ["indicator_freshness", "cache_stats", "performance_stats"],
        )
        _c.monitoring_manager.register_component(
            "market_data",
            _c.service,
            ["data_latency"],
        )
        _c.monitoring_manager.register_component(
            "economic_calendar",
            _c.economic_calendar_service,
            ["economic_calendar"],
        )
        _c.monitoring_manager.register_component(
            "signals",
            _c.signal_runtime,
            ["status"],
        )
        _c.monitoring_manager.start()
        _mark_startup_step("monitoring", "ready", current_started)
        _record_runtime_task_status(
            "startup",
            "monitoring",
            "ready",
            _startup_status["steps"]["monitoring"]["duration_ms"],
        )
        _c.health_monitor.record_metric(
            "system",
            "startup",
            1.0,
            {"version": "unified", "timestamp": "now"},
        )
        _startup_status["phase"] = "running"
        _startup_status["ready"] = True
        _startup_status["completed_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
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

        _shutdown_components()
        raise

    try:
        yield
    finally:
        _startup_status["phase"] = "stopping"
        _shutdown_components()
        if _c.health_monitor:
            _c.health_monitor.record_metric(
                "system",
                "shutdown",
                1.0,
                {"timestamp": "now"},
            )
        _startup_status["phase"] = "stopped"
        _startup_status["ready"] = False
