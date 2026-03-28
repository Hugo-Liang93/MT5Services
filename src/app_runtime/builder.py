"""App builder: construct all runtime components without starting threads."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from src.app_runtime.container import AppContainer
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
    load_db_settings,
    load_mt5_settings,
    load_storage_settings,
)
from src.monitoring import get_health_monitor, get_monitoring_manager
from src.monitoring.pipeline_event_bus import PipelineEventBus
from src.readmodels.runtime import RuntimeReadModel
from src.studio.runtime import build_studio_service

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


def _wire_margin_guard(
    position_manager: Any,
    trade_module: Any,
    trade_executor: Any = None,
) -> None:
    """Construct and inject MarginGuard into PositionManager and TradeExecutor."""
    try:
        from configparser import ConfigParser

        from src.risk.margin_guard import MarginGuard, load_margin_guard_config

        parser = ConfigParser()
        parser.read("config/risk.ini", encoding="utf-8")
        section = (
            dict(parser["margin_guard"])
            if parser.has_section("margin_guard")
            else {}
        )
        config = load_margin_guard_config(section)
        if not config.enabled:
            return

        def close_worst() -> dict:
            positions_fn = getattr(trade_module, "get_positions", None)
            close_fn = getattr(trade_module, "close_position", None)
            if not callable(positions_fn) or not callable(close_fn):
                return {"error": "no close capability"}
            positions = list(positions_fn())
            if not positions:
                return {"closed": None}
            worst = min(positions, key=lambda p: float(getattr(p, "profit", 0) or 0))
            ticket = int(getattr(worst, "ticket", 0))
            if ticket <= 0:
                return {"error": "invalid ticket"}
            result = close_fn(ticket, comment="margin_guard_emergency")
            return {"ticket": ticket, "result": result}

        def close_all() -> dict:
            fn = getattr(trade_module, "close_all_positions", None)
            if callable(fn):
                return fn(comment="margin_guard_emergency_all")
            return {"error": "no close_all capability"}

        def tighten_stops(factor: float) -> int:
            original = position_manager.trailing_atr_multiplier
            tightened = original * factor
            if tightened >= original:
                return 0
            position_manager.trailing_atr_multiplier = tightened
            logger.info(
                "MarginGuard: trailing ATR multiplier tightened %.2f -> %.2f",
                original,
                tightened,
            )
            with position_manager._lock:
                count = len(position_manager._positions)
            return count

        guard = MarginGuard(
            config,
            close_worst_fn=close_worst,
            close_all_fn=close_all,
            tighten_stops_fn=tighten_stops,
        )
        position_manager.set_margin_guard(guard)
        if trade_executor is not None and hasattr(trade_executor, "set_margin_guard"):
            trade_executor.set_margin_guard(guard)
        logger.info(
            "MarginGuard wired: warn=%.0f%% danger=%.0f%% critical=%.0f%% "
            "block=%.0f%% emergency=%.0f%%",
            config.warn_level,
            config.danger_level,
            config.critical_level,
            config.block_new_trades_level,
            config.emergency_close_level,
        )
    except Exception:
        logger.warning(
            "MarginGuard setup failed, continuing without margin monitoring",
            exc_info=True,
        )
