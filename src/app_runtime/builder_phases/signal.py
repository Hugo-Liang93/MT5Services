"""Signal system phase builders."""

from __future__ import annotations

from typing import Any

from src.app_runtime.container import AppContainer
from src.app_runtime.factories import (
    build_signal_components,
    register_signal_hot_reload,
)
from src.signals.contracts import StrategyCapability


def _resolve_optional_getter(target: Any, attr: str, default: Any = None) -> Any:
    if target is None:
        return default
    return getattr(target, attr, default)


def _validate_intrabar_trigger_coverage(
    signal_module: Any,
    signal_config: Any,
) -> None:
    """Validate intrabar capability coverage and emit warnings on missing trigger map."""
    if signal_module is None:
        return

    catalog_fn = _resolve_optional_getter(signal_module, "strategy_capability_catalog")
    if not callable(catalog_fn):
        return
    raw_catalog = catalog_fn()
    if raw_catalog is None:
        return

    if isinstance(raw_catalog, dict):
        raw_catalog_items = [raw_catalog]
    else:
        try:
            raw_catalog_items = list(raw_catalog)
        except TypeError:
            raw_catalog_items = [raw_catalog]

    catalog: list[StrategyCapability] = []
    for raw in raw_catalog_items:
        if isinstance(raw, StrategyCapability):
            catalog.append(raw)
        elif isinstance(raw, dict):
            capability = StrategyCapability.from_contract(raw)
            if capability is not None:
                catalog.append(capability)

    trigger_map: dict[str, str] = dict(
        _resolve_optional_getter(signal_config, "intrabar_trading_trigger_map", {}) or {}
    )
    strategy_timeframes: dict[str, Any] = dict(
        _resolve_optional_getter(signal_config, "strategy_timeframes", {}) or {}
    )
    for capability in catalog:
        name = capability.name
        if not capability.needs_intrabar:
            continue
        allowed_tfs = strategy_timeframes.get(name, ())
        if not allowed_tfs:
            continue
        for tf in allowed_tfs:
            tf_upper = str(tf).strip().upper()
            if tf_upper and tf_upper not in trigger_map:
                import logging

                logging.getLogger(__name__).warning(
                    "Strategy '%s' expects intrabar on %s but no trigger configured "
                    "in [intrabar_trading.trigger]. Intrabar events for this TF "
                    "will never arrive.",
                    name,
                    tf_upper,
                )


def _wire_margin_guard(
    position_manager: Any,
    trade_module: Any,
    trade_executor: Any = None,
) -> None:
    """Construct and inject MarginGuard into PositionManager and TradeExecutor."""
    from src.risk.runtime import wire_margin_guard

    try:
        guard = wire_margin_guard(
            position_manager=position_manager,
            trade_module=trade_module,
            trade_executor=trade_executor,
        )
        if guard is None:
            return
        import logging

        logger = logging.getLogger(__name__)
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
        import logging

        logging.getLogger(__name__).warning(
            "MarginGuard setup failed, continuing without margin monitoring",
            exc_info=True,
        )


def build_signal_layer(
    container: AppContainer,
    *,
    signal_config_loader: Any,
    signal_config: Any,
) -> None:
    """Build signal system services and wire runtime callbacks."""
    signal_components = build_signal_components(
        indicator_manager=container.indicator_manager,
        storage_writer=container.storage_writer,
        trade_module=container.trade_module,
        economic_calendar_service=container.economic_calendar_service,
        signal_config=signal_config,
        trading_state_store=container.trading_state_store,
        pipeline_event_bus=container.pipeline_event_bus,
    )
    container.calibrator = signal_components.calibrator
    container.regime_detector = signal_components.regime_detector
    container.market_structure_analyzer = signal_components.market_structure_analyzer
    container.signal_module = signal_components.signal_module
    container.signal_runtime = signal_components.signal_runtime
    container.htf_cache = signal_components.htf_cache
    container.signal_quality_tracker = signal_components.signal_quality_tracker
    container.trade_outcome_tracker = signal_components.trade_outcome_tracker
    container.exposure_closeout_controller = signal_components.exposure_closeout_controller
    container.position_manager = signal_components.position_manager
    container.trade_executor = signal_components.trade_executor
    _wire_margin_guard(
        container.position_manager,
        container.trade_module,
        container.trade_executor,
    )
    container.performance_tracker = signal_components.performance_tracker
    container.pending_entry_manager = signal_components.pending_entry_manager

    if container.ingestor is not None and signal_config.intrabar_trading_enabled:
        trigger_map = dict(signal_config.intrabar_trading_trigger_map)
        if trigger_map:
            container.ingestor.set_intrabar_trigger_map(trigger_map)

    _validate_intrabar_trigger_coverage(container.signal_module, signal_config)
    if container.signal_runtime is not None:
        container.signal_runtime.set_pipeline_event_bus(container.pipeline_event_bus)
        if container.ingestor is not None:
            ingestor = container.ingestor
            container.signal_runtime.set_warmup_ready_fn(
                lambda: not ingestor.is_backfilling
            )

    signal_hot_reload_cleanup = register_signal_hot_reload(
        container.signal_runtime,
        signal_config_loader,
        signal_module=container.signal_module,
        regime_detector=container.regime_detector,
        calibrator=container.calibrator,
        trade_executor=container.trade_executor,
        economic_calendar_service=container.economic_calendar_service,
        market_structure_analyzer=container.market_structure_analyzer,
        performance_tracker=container.performance_tracker,
        pending_entry_manager=container.pending_entry_manager,
    )
    container.shutdown_callbacks.append(signal_hot_reload_cleanup)
