from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .filters import BacktestFilterConfig, BacktestFilterSimulator

if TYPE_CHECKING:
    from .engine import BacktestEngine

logger = logging.getLogger(__name__)


def build_filter_simulator(engine: "BacktestEngine") -> BacktestFilterSimulator:
    """从 BacktestConfig 构建过滤器模拟器。"""
    economic_provider = None
    if engine._config.filter_economic_enabled:
        economic_provider = load_economic_provider(engine)

    filter_config = BacktestFilterConfig(
        enabled=engine._config.filters_enabled,
        session_filter_enabled=engine._config.filter_session_enabled,
        allowed_sessions=engine._config.filter_allowed_sessions,
        session_transition_enabled=engine._config.filter_session_transition_enabled,
        session_transition_cooldown_minutes=engine._config.filter_session_transition_cooldown,
        volatility_filter_enabled=engine._config.filter_volatility_enabled,
        volatility_spike_multiplier=engine._config.filter_volatility_spike_multiplier,
        spread_filter_enabled=engine._config.filter_spread_enabled,
        max_spread_points=engine._config.filter_max_spread_points,
        economic_filter_enabled=(
            engine._config.filter_economic_enabled and economic_provider is not None
        ),
        economic_provider=economic_provider,
        economic_lookahead_minutes=engine._config.filter_economic_lookahead_minutes,
        economic_lookback_minutes=engine._config.filter_economic_lookback_minutes,
        economic_importance_min=engine._config.filter_economic_importance_min,
    )
    return BacktestFilterSimulator(filter_config)


def load_economic_provider(engine: "BacktestEngine") -> Any:
    """从 DB 加载回测期间的经济事件，构建 BacktestTradeGuardProvider。"""
    try:
        from .economic_provider import (
            BacktestTradeGuardProvider,
            _SimpleSettings,
            load_backtest_economic_events,
        )
        from src.persistence.db import TimescaleWriter
        from src.config.database import load_db_settings
        from src.persistence.repositories.economic_repo import EconomicCalendarRepository
        from src.calendar.economic_calendar.trade_guard import infer_symbol_context

        settings = load_db_settings()
        writer = TimescaleWriter(settings, min_conn=1, max_conn=2)
        repo = EconomicCalendarRepository(writer)

        context = infer_symbol_context(engine._config.symbol)
        events = load_backtest_economic_events(
            economic_repo=repo,
            start_time=engine._config.start_time,
            end_time=engine._config.end_time,
            currencies=context["currencies"] or None,
            importance_min=engine._config.filter_economic_importance_min,
        )
        writer.close()

        if not events:
            logger.info("No economic events found for backtest period, economic filter disabled")
            return None

        try:
            from src.config.runtime import load_runtime_config

            rt_cfg = load_runtime_config()
            eco_settings = _SimpleSettings(
                trade_guard_relevance_filter_enabled=getattr(
                    rt_cfg, "trade_guard_relevance_filter_enabled", False
                ),
                gold_impact_keywords=getattr(rt_cfg, "gold_impact_keywords", ""),
            )
        except Exception:
            eco_settings = _SimpleSettings()

        return BacktestTradeGuardProvider(events, eco_settings)
    except Exception:
        logger.warning("Failed to load economic calendar for backtest", exc_info=True)
        return None
