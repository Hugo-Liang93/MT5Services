"""Signal system phase builders."""

from __future__ import annotations

from typing import Any

from src.app_runtime.builder_phases.tick_features import attach_tick_feature_listener
from src.app_runtime.container import AppContainer
from src.app_runtime.factories import (
    build_signal_components,
    register_signal_hot_reload,
)
from src.signals.contracts import StrategyCapability
from src.signals.orchestration.intrabar_contract import (
    intrabar_enabled_strategies,
    intrabar_trading_active,
)
from src.trading.execution.market_data_health import build_execution_market_data_health
from src.trading.execution.tick_feature_health import (
    build_execution_tick_feature_health,
)


def _resolve_optional_getter(target: Any, attr: str, default: Any = None) -> Any:
    if target is None:
        return default
    return getattr(target, attr, default)


def _validate_intrabar_trigger_coverage(
    signal_module: Any,
    signal_config: Any,
    *,
    effective_timeframes: tuple[str, ...] | list[str] | frozenset[str],
) -> None:
    """Validate intrabar trigger coverage against the effective runtime timeframe set."""
    if signal_module is None:
        return
    if not intrabar_trading_active(signal_config):
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

    configured_timeframes = frozenset(
        str(tf).strip().upper() for tf in effective_timeframes if str(tf).strip()
    )
    trigger_map: dict[str, str] = dict(
        (
            str(parent_tf).strip().upper(),
            str(trigger_tf).strip().upper(),
        )
        for parent_tf, trigger_tf in (
            _resolve_optional_getter(signal_config, "intrabar_trading_trigger_map", {})
            or {}
        ).items()
        if str(parent_tf).strip() and str(trigger_tf).strip()
    )
    strategy_timeframes: dict[str, Any] = dict(
        _resolve_optional_getter(signal_config, "strategy_timeframes", {}) or {}
    )
    enabled_intrabar_strategies = intrabar_enabled_strategies(signal_config)

    capabilities_by_name = {
        capability.name: capability for capability in catalog if capability.name
    }
    intrabar_capabilities = {
        capability.name: capability
        for capability in catalog
        if capability.name and capability.needs_intrabar
    }

    # 全局 enabled_strategies 可能包含其他 instance/environment 装载的策略；
    # 当前 instance 装载集是 capability catalog 的子集。enabled_strategies 中
    # 不在本 instance catalog 里的条目按"该 instance 不参与"处理（log + skip），
    # 不视为配置错——避免全局 intrabar 配置在 live/demo 之间反复编辑。
    not_loaded_here = sorted(
        strategy_name
        for strategy_name in enabled_intrabar_strategies
        if strategy_name not in capabilities_by_name
    )
    if not_loaded_here:
        import logging

        logging.getLogger(__name__).info(
            "intrabar_trading.enabled_strategies skips strategies not loaded in this "
            "instance (other env or candidate): %s",
            ", ".join(not_loaded_here),
        )
    enabled_intrabar_strategies = frozenset(
        strategy_name
        for strategy_name in enabled_intrabar_strategies
        if strategy_name in capabilities_by_name
    )

    unsupported_enabled = sorted(
        strategy_name
        for strategy_name in enabled_intrabar_strategies
        if strategy_name not in intrabar_capabilities
    )
    if unsupported_enabled:
        raise ValueError(
            "intrabar_trading.enabled_strategies contains strategies that do not "
            "declare intrabar capability: " + ", ".join(unsupported_enabled)
        )

    active_intrabar_strategies = sorted(enabled_intrabar_strategies)
    if active_intrabar_strategies and not trigger_map:
        raise ValueError(
            "intrabar_trading is enabled but [intrabar_trading.trigger] is empty"
        )

    errors: list[str] = []
    for strategy_name in active_intrabar_strategies:
        capability = intrabar_capabilities.get(strategy_name)
        if capability is None:
            continue
        configured_parent_tfs = tuple(
            str(tf).strip().upper()
            for tf in (strategy_timeframes.get(strategy_name) or ())
            if str(tf).strip()
        )
        candidate_parent_tfs = (
            configured_parent_tfs
            if configured_parent_tfs
            else tuple(sorted(configured_timeframes))
        )
        active_parent_tfs = tuple(
            tf for tf in candidate_parent_tfs if tf in configured_timeframes
        )
        if not active_parent_tfs:
            errors.append(
                f"strategy '{strategy_name}' declares intrabar but has no active parent "
                "timeframe inside app.ini[trading].timeframes"
            )
            continue
        for parent_tf in active_parent_tfs:
            child_tf = trigger_map.get(parent_tf)
            if not child_tf:
                errors.append(
                    f"strategy '{strategy_name}' requires intrabar on {parent_tf} but "
                    "no trigger is configured in [intrabar_trading.trigger]"
                )
                continue
            if child_tf not in configured_timeframes:
                errors.append(
                    f"strategy '{strategy_name}' requires intrabar on {parent_tf} via "
                    f"child timeframe {child_tf}, but {child_tf} is missing from "
                    "app.ini[trading].timeframes"
                )
    if errors:
        raise ValueError("Invalid intrabar runtime contract: " + "; ".join(errors))


def _wire_required_market_data_lanes(ingestor: Any, signal_runtime: Any) -> None:
    if ingestor is None or signal_runtime is None:
        return
    required_lanes_fn = _resolve_optional_getter(
        signal_runtime,
        "required_market_data_lanes",
    )
    set_required_lanes_fn = _resolve_optional_getter(
        ingestor,
        "set_required_market_data_lanes",
    )
    if not callable(required_lanes_fn) or not callable(set_required_lanes_fn):
        return
    set_required_lanes_fn(required_lanes_fn())


def _has_tick_derived_strategy(signal_runtime: Any) -> bool:
    if signal_runtime is None:
        return False
    contract_fn = _resolve_optional_getter(
        signal_runtime, "strategy_capability_contract"
    )
    if not callable(contract_fn):
        return False
    for item in contract_fn() or ():
        scopes = (
            item.valid_scopes
            if isinstance(item, StrategyCapability)
            else item.get("valid_scopes", ())
        )
        if "tick_derived" in tuple(scopes or ()):
            return True
    return False


def _wire_tick_feature_runtime(container: AppContainer) -> None:
    if container.signal_runtime is None:
        return
    if not _has_tick_derived_strategy(container.signal_runtime):
        return
    attach_tick_feature_listener(
        container,
        container.signal_runtime.enqueue_tick_feature_snapshot,
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
        runtime_identity=container.runtime_identity,
        trading_state_store=container.trading_state_store,
        pipeline_event_bus=container.pipeline_event_bus,
    )
    container.calibrator = signal_components.calibrator
    container.regime_detector = signal_components.regime_detector
    container.market_structure_analyzer = signal_components.market_structure_analyzer
    container.signal_module = signal_components.signal_module
    container.signal_runtime = signal_components.signal_runtime
    container.economic_decay_service = signal_components.economic_decay_service
    container.htf_cache = signal_components.htf_cache
    container.signal_quality_tracker = signal_components.signal_quality_tracker
    container.trade_outcome_tracker = signal_components.trade_outcome_tracker
    container.exposure_closeout_controller = (
        signal_components.exposure_closeout_controller
    )
    container.position_manager = signal_components.position_manager
    container.trade_executor = signal_components.trade_executor
    if container.position_manager is not None:
        _wire_margin_guard(
            container.position_manager,
            container.trade_module,
            container.trade_executor,
        )
    container.performance_tracker = signal_components.performance_tracker
    container.signal_performance_tracker = signal_components.signal_performance_tracker
    container.execution_performance_tracker = (
        signal_components.execution_performance_tracker
    )
    container.pending_entry_manager = signal_components.pending_entry_manager
    # ADR-013: registry 已在 PendingEntryManager 构造时由 factories/signals.py
    # 注入；此处取出绑定到 container 供 readmodel/API 端点访问。
    if container.pending_entry_manager is not None:
        container.entry_policy_registry = (
            container.pending_entry_manager.entry_policy_registry
        )
    container.execution_intent_publisher = signal_components.execution_intent_publisher
    container.execution_intent_consumer = signal_components.execution_intent_consumer

    if container.ingestor is not None and intrabar_trading_active(signal_config):
        trigger_map = dict(signal_config.intrabar_trading_trigger_map)
        if trigger_map:
            container.ingestor.set_intrabar_trigger_map(trigger_map)

    _wire_required_market_data_lanes(
        container.ingestor,
        container.signal_runtime,
    )
    _wire_tick_feature_runtime(container)
    if container.trade_executor is not None and container.ingestor is not None:
        ingestor = container.ingestor
        container.trade_executor.set_market_data_health_fn(
            lambda symbol: build_execution_market_data_health(ingestor, symbol)
        )
    if (
        container.trade_executor is not None
        and container.tick_feature_health_store is not None
    ):
        health_store = container.tick_feature_health_store
        feature_bus = container.tick_feature_bus
        container.trade_executor.set_tick_feature_health_fn(
            lambda symbol: build_execution_tick_feature_health(
                health_store,
                feature_bus,
                symbol,
            )
        )

    _validate_intrabar_trigger_coverage(
        container.signal_module,
        signal_config,
        effective_timeframes=tuple(container.indicator_manager.config.timeframes),
    )
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
        runtime_timeframes=tuple(container.indicator_manager.config.timeframes),
        regime_detector=container.regime_detector,
        calibrator=container.calibrator,
        trade_executor=container.trade_executor,
        economic_calendar_service=container.economic_calendar_service,
        market_structure_analyzer=container.market_structure_analyzer,
        performance_tracker=container.performance_tracker,
        signal_performance_tracker=container.signal_performance_tracker,
        execution_performance_tracker=container.execution_performance_tracker,
        execution_intent_publisher=container.execution_intent_publisher,
        pending_entry_manager=container.pending_entry_manager,
        ingestor=container.ingestor,
    )
    container.shutdown_callbacks.append(signal_hot_reload_cleanup)
