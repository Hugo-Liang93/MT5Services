"""Account runtime phase builders."""

from __future__ import annotations

from typing import Any

from src.app_runtime.container import AppContainer
from src.app_runtime.factories import (
    build_account_runtime_components,
    build_performance_tracker_config,
    register_signal_hot_reload,
)
from src.signals.evaluation.performance import StrategyPerformanceTracker
from src.signals.evaluation.regime import MarketRegimeDetector
from src.trading.execution.intrabar_guard import IntrabarTradeGuard


def build_account_runtime_layer(
    container: AppContainer,
    *,
    signal_config_loader: Any,
    signal_config: Any,
) -> None:
    """Build executor/main-account local account runtime without shared compute stack."""
    execution_performance_tracker = StrategyPerformanceTracker(
        config=build_performance_tracker_config(signal_config),
    )
    regime_detector = MarketRegimeDetector(
        adx_trending_threshold=signal_config.regime_adx_trending_threshold,
        adx_ranging_threshold=signal_config.regime_adx_ranging_threshold,
        bb_tight_pct=signal_config.regime_bb_tight_pct,
    )
    account_runtime = build_account_runtime_components(
        market_service=container.market_service,
        storage_writer=container.storage_writer,
        trade_module=container.trade_module,
        signal_config=signal_config,
        execution_performance_tracker=execution_performance_tracker,
        runtime_identity=container.runtime_identity,
        trading_state_store=container.trading_state_store,
        pipeline_event_bus=container.pipeline_event_bus,
        regime_detector=regime_detector,
        on_execution_skip=None,
    )
    container.regime_detector = regime_detector
    container.trade_outcome_tracker = account_runtime.trade_outcome_tracker
    container.exposure_closeout_controller = (
        account_runtime.exposure_closeout_controller
    )
    container.position_manager = account_runtime.position_manager
    container.trade_executor = account_runtime.trade_executor
    container.execution_performance_tracker = execution_performance_tracker
    container.pending_entry_manager = account_runtime.pending_entry_manager
    container.execution_intent_consumer = account_runtime.execution_intent_consumer

    if (
        container.trade_executor is not None
        and signal_config.intrabar_trading_enabled
        and signal_config.intrabar_trading_enabled_strategies
    ):
        container.trade_executor.set_intrabar_guard(IntrabarTradeGuard())

    from src.risk.runtime import wire_margin_guard

    wire_margin_guard(
        position_manager=container.position_manager,
        trade_module=container.trade_module,
        trade_executor=container.trade_executor,
    )

    signal_hot_reload_cleanup = register_signal_hot_reload(
        None,
        signal_config_loader,
        regime_detector=container.regime_detector,
        trade_executor=container.trade_executor,
        economic_calendar_service=container.economic_calendar_service,
        execution_performance_tracker=container.execution_performance_tracker,
        pending_entry_manager=container.pending_entry_manager,
    )
    container.shutdown_callbacks.append(signal_hot_reload_cleanup)
