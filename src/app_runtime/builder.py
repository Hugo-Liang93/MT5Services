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
from src.studio.service import StudioService
from src.studio import mappers as studio_mappers
from src.studio.models import build_event

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

        _ingestor_ref = c.ingestor
        c.market_impact_analyzer = MarketImpactAnalyzer(
            db_writer=c.storage_writer.db,
            market_repo=c.storage_writer.db.market_repo,
            settings=economic_settings,
            warmup_ready_fn=(
                (lambda: not _ingestor_ref.is_backfilling)
                if _ingestor_ref is not None
                else None
            ),
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
    _wire_margin_guard(c.position_manager, c.trade_module, c.trade_executor)
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

    # ── Phase 5: Studio (observability layer) ──
    c.studio_service = _build_studio_service(c)

    # ── Spread / cost sanity check ──
    _sig_cfg = signal_config_loader()
    if _sig_cfg.base_spread_points > 0:
        # 最小可能 SL ≈ base_spread × 3（极端低 ATR 场景的粗略下限）
        min_plausible_sl = _sig_cfg.base_spread_points * 3
        implied_ratio = _sig_cfg.base_spread_points / min_plausible_sl
        if implied_ratio > _sig_cfg.max_spread_to_stop_ratio * 0.8:
            logger.warning(
                "Spread/cost config may be too tight: base_spread=%.0f, "
                "max_spread_to_stop_ratio=%.2f. Low-ATR timeframes "
                "might reject most trades. Consider raising the ratio.",
                _sig_cfg.base_spread_points,
                _sig_cfg.max_spread_to_stop_ratio,
            )

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


# ── Studio wiring ─────────────────────────────────────────────
#
# This is the ONLY place that knows about both business modules and Studio
# mappers.  Each agent provider is a closure that captures a module reference
# and calls the corresponding pure mapper function.


def _build_studio_service(c: AppContainer) -> StudioService:
    studio = StudioService()

    # ── Register agent providers ──────────────────────────────
    # Each lambda captures the module reference; the mapper receives
    # only primitive data (dicts, bools) — never the module itself.

    if c.ingestor is not None:
        _ingestor = c.ingestor
        studio.register_agent(
            "collector",
            lambda: studio_mappers.map_collector(
                _ingestor.queue_stats(), _ingestor.is_backfilling
            ),
        )

    if c.indicator_manager is not None:
        _ind = c.indicator_manager
        studio.register_agent(
            "analyst",
            lambda: studio_mappers.map_analyst(_ind.get_performance_stats()),
        )
        _ms = c.market_service
        _ing = c.ingestor
        _tfs = list(c.indicator_manager.config.timeframes)
        studio.register_agent(
            "live_analyst",
            lambda: studio_mappers.map_live_analyst(
                _ind.get_performance_stats(),
                {tf: _ms.get_intrabar_series(None, tf) for tf in _tfs},
                _ing.queue_stats().get("intrabar", {}) if _ing else {},
            ),
        )

    if c.signal_module is not None:
        _sm = c.signal_module
        studio.register_agent(
            "strategist",
            lambda: studio_mappers.map_strategist(
                len(_sm.list_strategies()),
                _sm.recent_signals(limit=10, scope="confirmed"),
            ),
        )
        _sr_for_live = c.signal_runtime
        studio.register_agent(
            "live_strategist",
            lambda: studio_mappers.map_live_strategist(
                _sm.list_intrabar_strategies(),
                _sm.recent_signals(limit=20, scope="preview"),
                _sr_for_live.status() if _sr_for_live else {},
            ),
        )

    if c.signal_runtime is not None:
        _sr = c.signal_runtime
        studio.register_agent(
            "filter_guard",
            lambda: studio_mappers.map_filter_guard(_sr.status()),
        )
        studio.register_agent(
            "regime_guard",
            lambda: studio_mappers.map_regime_guard(_sr.status()),
        )
        studio.register_agent(
            "voter",
            lambda: studio_mappers.map_voter(_sr.status()),
        )

    if c.trade_executor is not None:
        _te_risk = c.trade_executor
        studio.register_agent(
            "risk_officer",
            lambda: studio_mappers.map_risk_officer(_te_risk.status()),
        )

    if c.trade_executor is not None:
        _te = c.trade_executor
        _pem = c.pending_entry_manager
        studio.register_agent(
            "trader",
            lambda: studio_mappers.map_trader(
                _te.status(),
                _pem.status() if _pem is not None else {},
            ),
        )

    if c.position_manager is not None:
        _pm = c.position_manager
        studio.register_agent(
            "position_manager",
            lambda: studio_mappers.map_position_manager(
                _pm.active_positions(), _pm.status()
            ),
        )

    if c.trade_module is not None:
        import dataclasses as _dc
        _tm = c.trade_module
        _pm_for_mg = c.position_manager
        studio.register_agent(
            "accountant",
            lambda: studio_mappers.map_accountant(
                _dc.asdict(_tm.account_info()) if _dc.is_dataclass(_tm.account_info()) else _tm.account_info(),
                _tm.trade_control_status(),
                margin_guard=_pm_for_mg._margin_guard.status() if _pm_for_mg is not None and _pm_for_mg._margin_guard is not None else None,
            ),
        )

    if c.economic_calendar_service is not None:
        _ecs = c.economic_calendar_service
        studio.register_agent(
            "calendar_reporter",
            lambda: studio_mappers.map_calendar_reporter(
                _ecs.stats(), _ecs.get_risk_windows()
            ),
        )

    if c.health_monitor is not None:
        _hm = c.health_monitor
        studio.register_agent(
            "inspector",
            lambda: studio_mappers.map_inspector(_hm.generate_report()),
        )

    # ── Register summary providers ────────────────────────────

    if c.trade_module is not None:
        _tm_s = c.trade_module
        studio.register_summary_provider(
            lambda: {
                "account": str((_tm_s.account_info() or {}).get("login", "")),
                "environment": "live",
            }
        )

    market_settings = get_runtime_market_settings()
    _default_symbol = market_settings.default_symbol
    studio.register_summary_provider(lambda: {"symbol": _default_symbol})

    # ── Register event listeners ────────────────────────────
    _register_studio_signal_listener(c, studio)
    _register_studio_trade_listener(c, studio)

    return studio


def _register_studio_signal_listener(
    c: AppContainer, studio: StudioService
) -> None:
    """Wire SignalRuntime's listener to push events into Studio."""
    if c.signal_runtime is None:
        return

    def _on_signal(event: Any) -> None:
        signal_state = getattr(event, "signal_state", "")
        symbol = getattr(event, "symbol", "")
        strategy = getattr(event, "strategy", "")
        direction = getattr(event, "direction", "")
        confidence = getattr(event, "confidence", 0.0)
        timeframe = getattr(event, "timeframe", "")

        if signal_state in ("confirmed_buy", "confirmed_sell"):
            studio.emit_event(build_event(
                "signal_generated",
                source="strategist",
                message=f"{symbol} {timeframe} {strategy} {direction} conf={confidence:.2f}",
                level="success",
                target="voter",
                symbol=symbol,
            ))
        elif signal_state == "confirmed_cancelled":
            studio.emit_event(build_event(
                "signal_generated",
                source="strategist",
                message=f"{symbol} {timeframe} {strategy} 信号取消",
                level="info",
                symbol=symbol,
            ))

    c.signal_runtime.add_signal_listener(_on_signal)


def _register_studio_trade_listener(
    c: AppContainer, studio: StudioService
) -> None:
    """Wire TradeExecutor's trade callback to push events into Studio."""
    if c.trade_executor is None:
        return

    def _on_trade(log_entry: Any) -> None:
        symbol = log_entry.get("symbol", "")
        direction = log_entry.get("direction", "")
        strategy = log_entry.get("strategy", "")
        params = log_entry.get("params", {})
        volume = params.get("volume", 0)

        studio.emit_event(build_event(
            "trade_executed",
            source="trader",
            message=f"{symbol} {direction} {volume} lot | {strategy}",
            level="success",
            target="position_manager",
            symbol=symbol,
        ))

    c.trade_executor.add_trade_listener(_on_trade)


def _wire_margin_guard(position_manager: Any, trade_module: Any, trade_executor: Any = None) -> None:
    """Construct and inject MarginGuard into PositionManager and TradeExecutor."""
    try:
        from configparser import ConfigParser
        from src.risk.margin_guard import MarginGuard, load_margin_guard_config

        parser = ConfigParser()
        parser.read("config/risk.ini", encoding="utf-8")
        section = dict(parser["margin_guard"]) if parser.has_section("margin_guard") else {}
        config = load_margin_guard_config(section)
        if not config.enabled:
            return

        # Callbacks for automatic actions
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
            # Reduce trailing ATR multiplier to factor * original
            original = position_manager.trailing_atr_multiplier
            tightened = original * factor
            if tightened >= original:
                return 0
            position_manager.trailing_atr_multiplier = tightened
            logger.info(
                "MarginGuard: trailing ATR multiplier tightened %.2f → %.2f",
                original, tightened,
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
            "MarginGuard wired: warn=%.0f%% danger=%.0f%% critical=%.0f%% block=%.0f%% emergency=%.0f%%",
            config.warn_level, config.danger_level, config.critical_level,
            config.block_new_trades_level, config.emergency_close_level,
        )
    except Exception:
        logger.warning("MarginGuard setup failed, continuing without margin monitoring", exc_info=True)
