"""Studio runtime assembly owned by the presentation layer."""

from __future__ import annotations

import dataclasses
from typing import Any

from src.app_runtime.container import AppContainer
from src.config import get_runtime_market_settings
from src.studio import mappers as studio_mappers
from src.studio.models import build_event
from src.studio.service import StudioService


def build_studio_service(container: AppContainer) -> StudioService:
    studio = StudioService()

    if container.ingestor is not None:
        ingestor = container.ingestor
        studio.register_agent(
            "collector",
            lambda: studio_mappers.map_collector(
                ingestor.queue_stats(), ingestor.is_backfilling
            ),
        )

    if container.indicator_manager is not None:
        indicator_manager = container.indicator_manager
        market_service = container.market_service
        ingestor = container.ingestor
        timeframes = list(indicator_manager.config.timeframes)
        studio.register_agent(
            "analyst",
            lambda: studio_mappers.map_analyst(indicator_manager.get_performance_stats()),
        )
        studio.register_agent(
            "live_analyst",
            lambda: studio_mappers.map_live_analyst(
                indicator_manager.get_performance_stats(),
                {tf: market_service.get_intrabar_series(None, tf) for tf in timeframes},
                ingestor.queue_stats().get("intrabar", {}) if ingestor else {},
            ),
        )

    if container.signal_module is not None:
        signal_module = container.signal_module
        signal_runtime = container.signal_runtime
        studio.register_agent(
            "strategist",
            lambda: studio_mappers.map_strategist(
                len(signal_module.list_strategies()),
                signal_module.recent_signals(limit=10, scope="confirmed"),
            ),
        )
        studio.register_agent(
            "live_strategist",
            lambda: studio_mappers.map_live_strategist(
                signal_module.list_intrabar_strategies(),
                signal_module.recent_signals(limit=20, scope="preview"),
                signal_runtime.status() if signal_runtime else {},
            ),
        )

    if container.signal_runtime is not None:
        signal_runtime = container.signal_runtime
        studio.register_agent(
            "filter_guard",
            lambda: studio_mappers.map_filter_guard(signal_runtime.status()),
        )
        studio.register_agent(
            "regime_guard",
            lambda: studio_mappers.map_regime_guard(signal_runtime.status()),
        )
        studio.register_agent(
            "voter",
            lambda: studio_mappers.map_voter(signal_runtime.status()),
        )

    if container.trade_executor is not None:
        trade_executor = container.trade_executor
        studio.register_agent(
            "risk_officer",
            lambda: studio_mappers.map_risk_officer(trade_executor.status()),
        )
        pending_entry_manager = container.pending_entry_manager
        studio.register_agent(
            "trader",
            lambda: studio_mappers.map_trader(
                trade_executor.status(),
                pending_entry_manager.status()
                if pending_entry_manager is not None
                else {},
            ),
        )

    if container.position_manager is not None:
        position_manager = container.position_manager
        studio.register_agent(
            "position_manager",
            lambda: studio_mappers.map_position_manager(
                position_manager.active_positions(),
                position_manager.status(),
            ),
        )

    if container.trade_module is not None:
        trade_module = container.trade_module
        position_manager = container.position_manager

        def _account_info_payload() -> Any:
            account_info = trade_module.account_info()
            if dataclasses.is_dataclass(account_info):
                return dataclasses.asdict(account_info)
            return account_info

        def _summary_provider() -> dict[str, Any]:
            account_info = _account_info_payload() or {}
            login = account_info.get("login", "") if isinstance(account_info, dict) else ""
            return {"account": str(login), "environment": "live"}

        studio.register_agent(
            "accountant",
            lambda: studio_mappers.map_accountant(
                _account_info_payload(),
                trade_module.trade_control_status(),
                margin_guard=(
                    position_manager.margin_guard_status()
                    if position_manager is not None
                    else None
                ),
            ),
        )
        studio.register_summary_provider(_summary_provider)

    if container.economic_calendar_service is not None:
        economic_calendar = container.economic_calendar_service
        studio.register_agent(
            "calendar_reporter",
            lambda: studio_mappers.map_calendar_reporter(
                economic_calendar.stats(),
                economic_calendar.get_risk_windows(),
            ),
        )

    if container.health_monitor is not None:
        health_monitor = container.health_monitor
        studio.register_agent(
            "inspector",
            lambda: studio_mappers.map_inspector(health_monitor.generate_report()),
        )

    market_settings = get_runtime_market_settings()
    studio.register_summary_provider(lambda: {"symbol": market_settings.default_symbol})

    signal_listener_cleanup = _register_studio_signal_listener(container, studio)
    if signal_listener_cleanup is not None:
        container.shutdown_callbacks.append(signal_listener_cleanup)
    trade_listener_cleanup = _register_studio_trade_listener(container, studio)
    if trade_listener_cleanup is not None:
        container.shutdown_callbacks.append(trade_listener_cleanup)

    return studio


def _register_studio_signal_listener(
    container: AppContainer, studio: StudioService
) -> Any:
    if container.signal_runtime is None:
        return None

    def _on_signal(event: Any) -> None:
        signal_state = getattr(event, "signal_state", "")
        symbol = getattr(event, "symbol", "")
        strategy = getattr(event, "strategy", "")
        direction = getattr(event, "direction", "")
        confidence = getattr(event, "confidence", 0.0)
        timeframe = getattr(event, "timeframe", "")

        if signal_state in ("confirmed_buy", "confirmed_sell"):
            studio.emit_event(
                build_event(
                    "signal_generated",
                    source="strategist",
                    message=f"{symbol} {timeframe} {strategy} {direction} conf={confidence:.2f}",
                    level="success",
                    target="voter",
                    symbol=symbol,
                )
            )
        elif signal_state == "confirmed_cancelled":
            studio.emit_event(
                build_event(
                    "signal_generated",
                    source="strategist",
                    message=f"{symbol} {timeframe} {strategy} 信号取消",
                    level="info",
                    symbol=symbol,
                )
            )

    container.signal_runtime.add_signal_listener(_on_signal)
    return lambda: container.signal_runtime.remove_signal_listener(_on_signal)


def _register_studio_trade_listener(
    container: AppContainer, studio: StudioService
) -> Any:
    if container.trade_executor is None:
        return None

    def _on_trade(log_entry: Any) -> None:
        symbol = log_entry.get("symbol", "")
        direction = log_entry.get("direction", "")
        strategy = log_entry.get("strategy", "")
        params = log_entry.get("params", {})
        volume = params.get("volume", 0)

        studio.emit_event(
            build_event(
                "trade_executed",
                source="trader",
                message=f"{symbol} {direction} {volume} lot | {strategy}",
                level="success",
                target="position_manager",
                symbol=symbol,
            )
        )

    container.trade_executor.add_trade_listener(_on_trade)
    return lambda: container.trade_executor.remove_trade_listener(_on_trade)
