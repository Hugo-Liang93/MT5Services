"""Studio runtime assembly owned by the presentation layer."""

from __future__ import annotations

import dataclasses
from typing import Any

from src.app_runtime.container import AppContainer
from src.backtesting.data import get_backtest_runtime_status
from src.config import get_runtime_market_settings
from src.studio import mappers as studio_mappers
from src.studio.models import build_event
from src.studio.service import StudioService


def build_studio_service(container: AppContainer) -> StudioService:
    studio = StudioService()
    trade_module = container.trade_module
    position_manager = container.position_manager
    economic_calendar = container.economic_calendar_service
    health_monitor = container.health_monitor

    def _account_info_payload() -> dict[str, Any]:
        if trade_module is None:
            return {}
        try:
            account_info = trade_module.account_info()
            if dataclasses.is_dataclass(account_info):
                return dataclasses.asdict(account_info)
            return dict(account_info or {})
        except Exception:
            return {}

    def _calendar_windows() -> list[dict[str, Any]]:
        if economic_calendar is None:
            return []
        try:
            return list(economic_calendar.get_risk_windows() or [])
        except Exception:
            return []

    def _calendar_stats() -> dict[str, Any]:
        if economic_calendar is None:
            return {}
        try:
            return dict(economic_calendar.stats() or {})
        except Exception:
            return {}

    def _inspector_report() -> dict[str, Any]:
        if health_monitor is None:
            return {}
        try:
            return dict(health_monitor.generate_report() or {})
        except Exception:
            return {}

    def _risk_support_evidence() -> dict[str, Any]:
        account_info = _account_info_payload()
        trade_control = (
            trade_module.trade_control_status() if trade_module is not None else {}
        )
        margin_guard = (
            position_manager.margin_guard_status()
            if position_manager is not None
            else {}
        )
        windows = _calendar_windows()
        inspector_report = _inspector_report()
        high_impact_active = [
            window
            for window in windows
            if str(window.get("impact", "")).lower() == "high"
            and bool(window.get("guard_active"))
        ]
        return {
            "accountant": {
                "balance": account_info.get("balance"),
                "equity": account_info.get("equity"),
                "free_margin": account_info.get("free_margin")
                or account_info.get("margin_free"),
                "close_only_mode": bool(trade_control.get("close_only_mode")),
                "auto_entry_enabled": bool(
                    trade_control.get("auto_entry_enabled", True)
                ),
                "margin_guard_state": margin_guard.get("state"),
            },
            "calendar_reporter": {
                "risk_window_count": len(windows),
                "high_impact_active": len(high_impact_active),
                "active_guard_labels": [
                    str(window.get("event_name") or "")
                    for window in high_impact_active[:3]
                    if str(window.get("event_name") or "").strip()
                ],
                "stale": _calendar_stats().get("stale"),
            },
            "inspector": {
                "overall_status": inspector_report.get("overall_status"),
                "active_alert_count": len(inspector_report.get("active_alerts", [])),
            },
            "performance_tracker": {
                "pnl_circuit_paused": (
                    container.performance_tracker.is_trading_paused()
                    if container.performance_tracker is not None
                    else False
                ),
            },
            "position_manager": {
                "is_after_eod": (
                    position_manager.is_after_eod_today()
                    if position_manager is not None
                    and hasattr(position_manager, "is_after_eod_today")
                    else False
                ),
            },
        }

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
                signal_runtime.status() if signal_runtime else {},
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
            lambda: studio_mappers.map_risk_officer(
                trade_executor.status(),
                support_evidence=_risk_support_evidence(),
            ),
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
        def _summary_provider() -> dict[str, Any]:
            account_info = _account_info_payload() or {}
            login = account_info.get("login", "")
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
        studio.register_agent(
            "calendar_reporter",
            lambda: studio_mappers.map_calendar_reporter(
                _calendar_stats(),
                _calendar_windows(),
            ),
        )

    studio.register_agent(
        "backtester",
        lambda: studio_mappers.map_backtester(get_backtest_runtime_status()),
    )

    if container.paper_trading_bridge is not None:
        _pt_bridge = container.paper_trading_bridge
        studio.register_agent(
            "paper_trader",
            lambda: studio_mappers.map_paper_trader(_pt_bridge.status()),
        )

    if container.health_monitor is not None:
        studio.register_agent(
            "inspector",
            lambda: studio_mappers.map_inspector(_inspector_report()),
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

    def _is_vote_result_strategy(strategy_name: str) -> bool:
        policy = container.signal_runtime.policy
        voting_group_names = {
            group.name
            for group in getattr(policy, "voting_groups", []) or []
            if getattr(group, "name", "")
        }
        if strategy_name in voting_group_names:
            return True
        return bool(
            getattr(policy, "voting_enabled", False)
            and not getattr(policy, "voting_groups", [])
            and strategy_name == "consensus"
        )

    def _on_signal(event: Any) -> None:
        signal_state = getattr(event, "signal_state", "")
        symbol = getattr(event, "symbol", "")
        strategy = getattr(event, "strategy", "")
        direction = getattr(event, "direction", "")
        confidence = getattr(event, "confidence", 0.0)
        timeframe = getattr(event, "timeframe", "")
        source = "voter" if _is_vote_result_strategy(strategy) else "strategist"
        target = "risk_officer"

        if signal_state in ("confirmed_buy", "confirmed_sell"):
            studio.emit_event(
                build_event(
                    "signal_generated",
                    source=source,
                    message=f"{symbol} {timeframe} {strategy} {direction} conf={confidence:.2f}",
                    level="success",
                    target=target,
                    symbol=symbol,
                )
            )
        elif signal_state == "confirmed_cancelled":
            studio.emit_event(
                build_event(
                    "signal_generated",
                    source=source,
                    message=f"{symbol} {timeframe} {strategy} 已取消",
                    level="info",
                    target=target,
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
