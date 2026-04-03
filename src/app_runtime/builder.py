"""App builder: construct all runtime components without starting threads."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from src.app_runtime.container import AppContainer
from src.app_runtime.lifecycle import (
    FunctionalRuntimeComponent,
    RuntimeComponentRegistry,
)
from src.app_runtime.mode_controller import (
    RuntimeModeController,
)
from src.app_runtime.mode_policy import (
    RuntimeMode,
    RuntimeModeAutoTransitionPolicy,
    RuntimeModeEODAction,
    RuntimeModePolicy,
    RuntimeModeTransitionGuard,
)
from src.app_runtime.factories import (
    build_signal_components,
    build_trading_components,
    create_indicator_manager,
    create_ingestor,
    create_market_service,
    create_storage_writer,
    register_signal_hot_reload,
)
from src.config import (
    get_economic_config,
    get_effective_config_snapshot,
    get_risk_config,
    get_runtime_data_path,
    get_runtime_ingest_settings,
    get_runtime_market_settings,
    get_signal_config,
    get_trading_ops_config,
    load_db_settings,
    load_mt5_settings,
    load_storage_settings,
)
from src.monitoring import get_health_monitor, get_monitoring_manager
from src.monitoring.pipeline_event_bus import PipelineEventBus
from src.readmodels.runtime import RuntimeReadModel
from src.studio.runtime import build_studio_service
from src.trading.state_alerts import TradingStateAlerts
from src.trading.state_recovery import TradingStateRecovery
from src.trading.state_recovery_policy import TradingStateRecoveryPolicy
from src.trading.state_store import TradingStateStore

logger = logging.getLogger(__name__)


def _enum_or_raw(value: Any) -> str:
    return getattr(value, "value", value)


def build_app_container(
    *,
    signal_config_loader: Any = None,
) -> AppContainer:
    """Build all components and wire dependencies. No threads are started."""
    if signal_config_loader is None:
        signal_config_loader = get_signal_config

    container = AppContainer()
    ingest_settings = get_runtime_ingest_settings()
    market_settings = get_runtime_market_settings()

    # Phase 1: market data
    container.market_service = create_market_service(
        load_mt5_settings(), market_settings
    )
    container.storage_writer = create_storage_writer(
        load_db_settings(), load_storage_settings()
    )
    container.market_service.attach_storage(container.storage_writer)
    container.ingestor = create_ingestor(
        container.market_service,
        container.storage_writer,
        ingest_settings,
    )
    container.indicator_manager = create_indicator_manager(
        container.market_service,
        container.storage_writer,
    )

    # Pipeline tracing
    container.pipeline_event_bus = PipelineEventBus()
    container.indicator_manager._pipeline_event_bus = container.pipeline_event_bus
    container.indicator_manager._current_trace_id = None

    # Phase 2: trading and economic calendar
    trading_components = build_trading_components(
        container.storage_writer,
        get_economic_config(),
    )
    container.economic_calendar_service = (
        trading_components.economic_calendar_service
    )
    container.trade_registry = trading_components.trade_registry
    container.trade_module = trading_components.trade_module
    default_account_alias: Optional[str] = trading_components.default_account_alias
    if container.trade_module is not None and container.storage_writer is not None:
        trading_ops_config = get_trading_ops_config()
        container.trading_state_store = TradingStateStore(
            container.storage_writer.db,
            account_alias_getter=lambda: container.trade_module.active_account_alias,
        )
        container.trading_state_alerts = TradingStateAlerts(
            state_store=container.trading_state_store,
            trading_module=container.trade_module,
            account_alias_getter=lambda: container.trade_module.active_account_alias,
        )
        container.trading_state_recovery_policy = TradingStateRecoveryPolicy(
            container.trading_state_store,
            orphan_action=trading_ops_config.pending_recovery_orphan_action,
            missing_action=trading_ops_config.pending_recovery_missing_action,
        )
        container.trading_state_recovery = TradingStateRecovery(
            container.trading_state_store,
            policy=container.trading_state_recovery_policy,
        )
        container.trade_module.set_trade_control_update_hook(
            container.trading_state_store.sync_trade_control
        )

    economic_settings = get_economic_config()
    if economic_settings.market_impact_enabled:
        from src.calendar.economic_calendar.market_impact import MarketImpactAnalyzer

        ingestor_ref = container.ingestor
        container.market_impact_analyzer = MarketImpactAnalyzer(
            db_writer=container.storage_writer.db,
            market_repo=container.storage_writer.db.market_repo,
            settings=economic_settings,
            warmup_ready_fn=(
                (lambda: not ingestor_ref.is_backfilling)
                if ingestor_ref is not None
                else None
            ),
        )
        container.economic_calendar_service.market_impact_analyzer = (
            container.market_impact_analyzer
        )

    # Phase 3: signal system
    signal_components = build_signal_components(
        indicator_manager=container.indicator_manager,
        storage_writer=container.storage_writer,
        trade_module=container.trade_module,
        economic_calendar_service=container.economic_calendar_service,
        signal_config=signal_config_loader(),
        trading_state_store=container.trading_state_store,
    )
    container.calibrator = signal_components.calibrator
    container.market_structure_analyzer = (
        signal_components.market_structure_analyzer
    )
    container.signal_module = signal_components.signal_module
    container.signal_runtime = signal_components.signal_runtime
    container.htf_cache = signal_components.htf_cache
    container.signal_quality_tracker = signal_components.signal_quality_tracker
    container.trade_outcome_tracker = signal_components.trade_outcome_tracker
    container.exposure_closeout_controller = (
        signal_components.exposure_closeout_controller
    )
    container.position_manager = signal_components.position_manager
    container.trade_executor = signal_components.trade_executor
    _wire_margin_guard(
        container.position_manager,
        container.trade_module,
        container.trade_executor,
    )
    container.performance_tracker = signal_components.performance_tracker
    container.pending_entry_manager = signal_components.pending_entry_manager
    if container.signal_runtime is not None:
        container.signal_runtime._pipeline_event_bus = container.pipeline_event_bus
        if container.ingestor is not None:
            ingestor = container.ingestor
            container.signal_runtime._warmup_ready_fn = (
                lambda: not ingestor.is_backfilling
            )
    signal_hot_reload_cleanup = register_signal_hot_reload(
        container.signal_runtime,
        signal_config_loader,
        trade_executor=container.trade_executor,
        economic_calendar_service=container.economic_calendar_service,
        market_structure_analyzer=container.market_structure_analyzer,
        performance_tracker=container.performance_tracker,
        pending_entry_manager=container.pending_entry_manager,
    )
    container.shutdown_callbacks.append(signal_hot_reload_cleanup)

    if container.trade_module is not None:
        trading_ops_config = get_trading_ops_config()
        container.runtime_component_registry = _build_runtime_component_registry(
            container,
            signal_config_loader=signal_config_loader,
        )
        container.runtime_mode_guard = RuntimeModeTransitionGuard(
            trading_module_getter=lambda: container.trade_module,
        )
        container.runtime_mode_auto_policy = RuntimeModeAutoTransitionPolicy(
            after_eod_action=RuntimeModeEODAction(
                trading_ops_config.runtime_mode_after_eod
            ),
        )
        container.runtime_mode_controller = RuntimeModeController(
            container,
            policy=RuntimeModePolicy(
                initial_mode=RuntimeMode(trading_ops_config.runtime_mode),
                after_eod_action=container.runtime_mode_auto_policy.after_eod_action,
                auto_check_interval_seconds=(
                    trading_ops_config.runtime_mode_auto_check_interval_seconds
                ),
            ),
            guard=container.runtime_mode_guard,
            auto_transition_policy=container.runtime_mode_auto_policy,
        )

    # Phase 4: monitoring
    container.health_monitor = get_health_monitor(
        get_runtime_data_path("health_monitor.db")
    )
    container.health_monitor.configure_alerts(
        data_latency_warning=max(1.0, ingest_settings.max_allowed_delay / 2.0),
        data_latency_critical=max(1.0, ingest_settings.max_allowed_delay),
    )
    container.health_monitor.alerts["economic_calendar_staleness"] = {
        "warning": max(1.0, economic_settings.stale_after_seconds / 2.0),
        "critical": max(1.0, economic_settings.stale_after_seconds),
    }
    container.health_monitor.alerts["economic_provider_failures"] = {
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
    container.monitoring_manager = get_monitoring_manager(
        container.health_monitor,
        check_interval=monitoring_interval,
    )
    container.health_monitor.cleanup_old_data(days_to_keep=30)
    container.indicator_manager.cleanup_old_events(days_to_keep=7)

    # Phase 5: runtime read-models
    container.runtime_read_model = RuntimeReadModel(
        health_monitor=container.health_monitor,
        ingestor=container.ingestor,
        indicator_manager=container.indicator_manager,
        trading_service=container.trade_module,
        signal_runtime=container.signal_runtime,
        trade_executor=container.trade_executor,
        position_manager=container.position_manager,
        pending_entry_manager=container.pending_entry_manager,
        trading_state_store=container.trading_state_store,
        trading_state_alerts=container.trading_state_alerts,
        exposure_closeout_controller=container.exposure_closeout_controller,
        runtime_mode_controller=container.runtime_mode_controller,
    )

    # Phase 6: frontend observability
    container.studio_service = build_studio_service(container)

    # Spread / cost sanity check
    signal_config = signal_config_loader()
    if signal_config.base_spread_points > 0:
        min_plausible_sl = signal_config.base_spread_points * 3
        implied_ratio = signal_config.base_spread_points / min_plausible_sl
        if implied_ratio > signal_config.max_spread_to_stop_ratio * 0.8:
            logger.warning(
                "Spread/cost config may be too tight: base_spread=%.0f, "
                "max_spread_to_stop_ratio=%.2f. Low-ATR timeframes "
                "might reject most trades. Consider raising the ratio.",
                signal_config.base_spread_points,
                signal_config.max_spread_to_stop_ratio,
            )

    logger.info(
        "Effective runtime config: %s",
        json.dumps(
            {
                **get_effective_config_snapshot(),
                "risk": get_risk_config().model_dump(),
                "active_trading_account": default_account_alias,
                "trading_account": (
                    container.trade_module.list_accounts()[0]
                    if container.trade_module
                    else None
                ),
                "indicator_scope": {
                    "symbols": list(container.indicator_manager.config.symbols),
                    "timeframes": list(container.indicator_manager.config.timeframes),
                    "inherit_symbols": container.indicator_manager.config.inherit_symbols,
                    "inherit_timeframes": (
                        container.indicator_manager.config.inherit_timeframes
                    ),
                    "indicator_reload_interval": (
                        container.indicator_manager.config.reload_interval
                    ),
                    "indicator_poll_interval": (
                        container.indicator_manager.config.pipeline.poll_interval
                    ),
                    "indicator_cache_maxsize": (
                        container.indicator_manager.config.pipeline.cache_maxsize
                    ),
                    "indicator_cache_strategy": _enum_or_raw(
                        container.indicator_manager.config.pipeline.cache_strategy
                    ),
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )

    return container


def _build_runtime_component_registry(
    container: AppContainer,
    *,
    signal_config_loader,
) -> RuntimeComponentRegistry:
    all_modes = frozenset(mode.value for mode in RuntimeMode)
    signal_modes = frozenset({RuntimeMode.FULL.value, RuntimeMode.OBSERVE.value})
    risk_modes = frozenset(
        {
            RuntimeMode.FULL.value,
            RuntimeMode.OBSERVE.value,
            RuntimeMode.RISK_OFF.value,
        }
    )
    listener_state = {"attached": False}

    def _start_indicator_stack() -> None:
        indicators = container.indicator_manager
        if indicators is not None:
            indicators.start()
        calibrator = container.calibrator
        if calibrator is not None:
            try:
                calibrator_cache_path = get_runtime_data_path("calibrator_cache.json")
                os.makedirs(os.path.dirname(calibrator_cache_path), exist_ok=True)
                calibrator.load(calibrator_cache_path)
            except Exception:
                logger.debug("Calibrator warm-start load failed", exc_info=True)
            try:
                calibrator.start_background_refresh()
            except Exception:
                logger.debug("Calibrator start failed", exc_info=True)
        calendar = container.economic_calendar_service
        if calendar is not None:
            calendar.start()

    def _stop_indicator_stack() -> None:
        calibrator = container.calibrator
        if calibrator is not None:
            try:
                calibrator.stop_background_refresh()
            except Exception:
                logger.debug("Calibrator stop failed", exc_info=True)
        calendar = container.economic_calendar_service
        if calendar is not None:
            calendar.stop()
        indicators = container.indicator_manager
        if indicators is not None:
            indicators.shutdown()

    def _start_signals() -> None:
        runtime = container.signal_runtime
        if runtime is not None:
            runtime.start()

    def _stop_signals() -> None:
        runtime = container.signal_runtime
        if runtime is not None:
            runtime.stop()

    def _start_trade_execution() -> None:
        runtime = container.signal_runtime
        executor = container.trade_executor
        if runtime is None or executor is None:
            return
        executor.start()
        if not listener_state["attached"]:
            runtime.add_signal_listener(executor.on_signal_event)
            listener_state["attached"] = True

    def _stop_trade_execution() -> None:
        runtime = container.signal_runtime
        executor = container.trade_executor
        if runtime is None or executor is None:
            return
        if listener_state["attached"]:
            runtime.remove_signal_listener(executor.on_signal_event)
            listener_state["attached"] = False
        executor.stop()

    def _start_pending_entry() -> None:
        pending = container.pending_entry_manager
        if pending is None:
            return
        was_running = pending.is_running()
        pending.start()
        recovery = container.trading_state_recovery
        trading = container.trade_module
        if not was_running and recovery is not None and trading is not None:
            try:
                result = recovery.restore_pending_orders(
                    pending_entry_manager=pending,
                    trading_module=trading,
                )
                logger.info("Pending order recovery: %s", result)
            except Exception:
                logger.warning("Pending order restore failed", exc_info=True)

    def _start_position_manager() -> None:
        position_manager = container.position_manager
        if position_manager is None:
            return
        was_running = position_manager.is_running()
        reconcile_interval = float(signal_config_loader().position_reconcile_interval)
        position_manager.start(reconcile_interval=reconcile_interval)
        if not was_running:
            try:
                recovery_result = position_manager.sync_open_positions()
                logger.info("PositionManager mode sync: %s", recovery_result)
            except Exception:
                logger.warning("PositionManager mode sync failed", exc_info=True)
            try:
                overnight_result = position_manager.force_close_overnight()
                if overnight_result is not None:
                    logger.warning(
                        "Overnight force close on mode start: %s",
                        overnight_result,
                    )
            except Exception:
                logger.warning("Overnight force close failed", exc_info=True)

    return RuntimeComponentRegistry(
        [
            FunctionalRuntimeComponent(
                name="storage",
                supported_modes=all_modes,
                start_fn=lambda: (
                    container.storage_writer.start()
                    if container.storage_writer is not None
                    else None
                ),
                stop_fn=lambda: (
                    container.storage_writer.stop()
                    if container.storage_writer is not None
                    else None
                ),
                is_running_fn=lambda: bool(
                    container.storage_writer is not None
                    and container.storage_writer.is_running()
                ),
            ),
            FunctionalRuntimeComponent(
                name="ingestion",
                supported_modes=all_modes,
                start_fn=lambda: (
                    container.ingestor.start()
                    if container.ingestor is not None
                    else None
                ),
                stop_fn=lambda: (
                    container.ingestor.stop()
                    if container.ingestor is not None
                    else None
                ),
                is_running_fn=lambda: bool(
                    container.ingestor is not None
                    and container.ingestor.is_running()
                ),
            ),
            FunctionalRuntimeComponent(
                name="indicators",
                supported_modes=signal_modes,
                start_fn=_start_indicator_stack,
                stop_fn=_stop_indicator_stack,
                is_running_fn=lambda: bool(
                    container.indicator_manager is not None
                    and container.indicator_manager.is_running()
                ),
            ),
            FunctionalRuntimeComponent(
                name="signals",
                supported_modes=signal_modes,
                start_fn=_start_signals,
                stop_fn=_stop_signals,
                is_running_fn=lambda: bool(
                    container.signal_runtime is not None
                    and container.signal_runtime.is_running()
                ),
            ),
            FunctionalRuntimeComponent(
                name="trade_execution",
                supported_modes=frozenset({RuntimeMode.FULL.value}),
                start_fn=_start_trade_execution,
                stop_fn=_stop_trade_execution,
                is_running_fn=lambda: bool(listener_state["attached"]),
            ),
            FunctionalRuntimeComponent(
                name="pending_entry",
                supported_modes=risk_modes,
                start_fn=_start_pending_entry,
                stop_fn=lambda: (
                    container.pending_entry_manager.shutdown()
                    if container.pending_entry_manager is not None
                    else None
                ),
                is_running_fn=lambda: bool(
                    container.pending_entry_manager is not None
                    and container.pending_entry_manager.is_running()
                ),
            ),
            FunctionalRuntimeComponent(
                name="position_manager",
                supported_modes=risk_modes,
                start_fn=_start_position_manager,
                stop_fn=lambda: (
                    container.position_manager.stop()
                    if container.position_manager is not None
                    else None
                ),
                is_running_fn=lambda: bool(
                    container.position_manager is not None
                    and container.position_manager.is_running()
                ),
            ),
        ]
    )


def _wire_margin_guard(
    position_manager: Any,
    trade_module: Any,
    trade_executor: Any = None,
) -> None:
    """Construct and inject MarginGuard into PositionManager and TradeExecutor."""
    try:
        from src.risk.runtime import wire_margin_guard

        guard = wire_margin_guard(
            position_manager=position_manager,
            trade_module=trade_module,
            trade_executor=trade_executor,
        )
        if guard is None:
            return
        logger.info(
            "MarginGuard wired: warn=%.0f%% danger=%.0f%% critical=%.0f%% "
            "block=%.0f%% emergency=%.0f%%",
            guard.config.warn_level,
            guard.config.danger_level,
            guard.config.critical_level,
            guard.config.block_new_trades_level,
            guard.config.emergency_close_level,
        )
    except Exception:
        logger.warning(
            "MarginGuard setup failed, continuing without margin monitoring",
            exc_info=True,
        )
