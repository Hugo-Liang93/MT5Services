"""AppBuilder — construct all components without starting any threads."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from src.api.factories import (
    build_signal_components,
    build_trading_components,
    create_indicator_manager,
    create_ingestor,
    create_market_service,
    create_storage_writer,
    register_signal_hot_reload,
)
from src.app_runtime.container import AppContainer
from src.monitoring.pipeline_event_bus import PipelineEventBus
from src.config import (
    get_economic_config,
    get_effective_config_snapshot,
    get_risk_config,
    get_runtime_ingest_settings,
    get_runtime_market_settings,
    get_signal_config,
    load_db_settings,
    load_mt5_settings,
    load_storage_settings,
)
from src.monitoring import get_health_monitor, get_monitoring_manager

logger = logging.getLogger(__name__)


def _enum_or_raw(value: Any) -> str:
    return getattr(value, "value", value)


def build_app_container(
    *,
    signal_config_loader: Any = None,
) -> AppContainer:
    """Build all components and wire dependencies. No threads are started.

    Parameters
    ----------
    signal_config_loader:
        Callable that returns a fresh SignalConfig. Used for hot-reload
        registration. Defaults to :func:`get_signal_config`.

    Returns
    -------
    AppContainer
        Fully wired container ready for :class:`AppRuntime` to start.
    """
    if signal_config_loader is None:
        signal_config_loader = get_signal_config

    c = AppContainer()
    ingest_settings = get_runtime_ingest_settings()
    market_settings = get_runtime_market_settings()

    # ── Phase 1: Market data layer ──
    c.market_service = create_market_service(load_mt5_settings(), market_settings)
    c.storage_writer = create_storage_writer(load_db_settings(), load_storage_settings())
    c.market_service.attach_storage(c.storage_writer)
    c.ingestor = create_ingestor(c.market_service, c.storage_writer, ingest_settings)
    c.indicator_manager = create_indicator_manager(c.market_service, c.storage_writer)

    # ── Pipeline tracing (read-only observer bus) ──
    c.pipeline_event_bus = PipelineEventBus()
    c.indicator_manager._pipeline_event_bus = c.pipeline_event_bus
    c.indicator_manager._current_trace_id = None  # thread-local scratch slot

    # ── Phase 2: Trading & calendar ──
    trading_components = build_trading_components(c.storage_writer, get_economic_config())
    c.economic_calendar_service = trading_components.economic_calendar_service
    c.trade_registry = trading_components.trade_registry
    c.trade_module = trading_components.trade_module
    default_account_alias: Optional[str] = trading_components.default_account_alias

    economic_settings = get_economic_config()
    if economic_settings.market_impact_enabled:
        from src.calendar.economic_calendar.market_impact import MarketImpactAnalyzer

        c.market_impact_analyzer = MarketImpactAnalyzer(
            db_writer=c.storage_writer.db,
            market_repo=c.storage_writer.db.market_repo,
            settings=economic_settings,
        )
        c.economic_calendar_service.market_impact_analyzer = c.market_impact_analyzer

    # ── Phase 3: Signal system ──
    signal_components = build_signal_components(
        indicator_manager=c.indicator_manager,
        storage_writer=c.storage_writer,
        trade_module=c.trade_module,
        economic_calendar_service=c.economic_calendar_service,
        signal_config=signal_config_loader(),
    )
    c.calibrator = signal_components.calibrator
    c.market_structure_analyzer = signal_components.market_structure_analyzer
    c.signal_module = signal_components.signal_module
    c.signal_runtime = signal_components.signal_runtime
    c.htf_cache = signal_components.htf_cache
    c.signal_quality_tracker = signal_components.signal_quality_tracker
    c.trade_outcome_tracker = signal_components.trade_outcome_tracker
    c.position_manager = signal_components.position_manager
    c.trade_executor = signal_components.trade_executor
    c.performance_tracker = signal_components.performance_tracker
    c.pending_entry_manager = signal_components.pending_entry_manager
    if c.signal_runtime is not None:
        c.signal_runtime._pipeline_event_bus = c.pipeline_event_bus
        # 显式 warmup 屏障：信号评估在 ingestor 回补完成前被阻塞。
        # 使用 lambda 延迟求值，避免构建阶段的顺序耦合。
        if c.ingestor is not None:
            _ingestor = c.ingestor  # capture narrowed reference for lambda
            c.signal_runtime._warmup_ready_fn = lambda: not _ingestor.is_backfilling
    register_signal_hot_reload(
        c.signal_runtime,
        signal_config_loader,
        trade_executor=c.trade_executor,
        economic_calendar_service=c.economic_calendar_service,
        market_structure_analyzer=c.market_structure_analyzer,
        performance_tracker=c.performance_tracker,
        pending_entry_manager=c.pending_entry_manager,
    )

    # ── Phase 4: Monitoring ──
    c.health_monitor = get_health_monitor("health_monitor.db")
    c.health_monitor.configure_alerts(
        data_latency_warning=max(1.0, ingest_settings.max_allowed_delay / 2.0),
        data_latency_critical=max(1.0, ingest_settings.max_allowed_delay),
    )
    c.health_monitor.alerts["economic_calendar_staleness"] = {
        "warning": max(1.0, economic_settings.stale_after_seconds / 2.0),
        "critical": max(1.0, economic_settings.stale_after_seconds),
    }
    c.health_monitor.alerts["economic_provider_failures"] = {
        "warning": 1.0,
        "critical": float(max(2, economic_settings.request_retries)),
    }
    monitoring_interval = max(
        1,
        int(min(ingest_settings.health_check_interval, ingest_settings.queue_monitor_interval)),
    )
    c.monitoring_manager = get_monitoring_manager(c.health_monitor, check_interval=monitoring_interval)
    c.health_monitor.cleanup_old_data(days_to_keep=30)
    c.indicator_manager.cleanup_old_events(days_to_keep=7)

    # ── Log effective config ──
    logger.info(
        "Effective runtime config: %s",
        json.dumps(
            {
                **get_effective_config_snapshot(),
                "risk": get_risk_config().model_dump(),
                "active_trading_account": default_account_alias,
                "trading_account": (
                    c.trade_module.list_accounts()[0] if c.trade_module else None
                ),
                "indicator_scope": {
                    "symbols": list(c.indicator_manager.config.symbols),
                    "timeframes": list(c.indicator_manager.config.timeframes),
                    "inherit_symbols": c.indicator_manager.config.inherit_symbols,
                    "inherit_timeframes": c.indicator_manager.config.inherit_timeframes,
                    "indicator_reload_interval": c.indicator_manager.config.reload_interval,
                    "indicator_poll_interval": c.indicator_manager.config.pipeline.poll_interval,
                    "indicator_cache_maxsize": c.indicator_manager.config.pipeline.cache_maxsize,
                    "indicator_cache_strategy": _enum_or_raw(
                        c.indicator_manager.config.pipeline.cache_strategy
                    ),
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )

    return c
