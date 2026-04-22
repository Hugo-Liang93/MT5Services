from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .simulator import BacktestFilterConfig, BacktestFilterSimulator

if TYPE_CHECKING:
    from ..engine.runner import BacktestEngine

logger = logging.getLogger(__name__)


def build_filter_simulator(engine: "BacktestEngine") -> BacktestFilterSimulator:
    """从 BacktestConfig 构建过滤器模拟器。"""
    f = engine._config.filters
    economic_provider = None
    if f.economic_enabled:
        economic_provider = load_economic_provider(engine)

    filter_config = BacktestFilterConfig(
        enabled=f.enabled,
        session_filter_enabled=f.session_enabled,
        allowed_sessions=f.allowed_sessions,
        session_transition_enabled=f.session_transition_enabled,
        session_transition_cooldown_minutes=f.session_transition_cooldown,
        volatility_filter_enabled=f.volatility_enabled,
        volatility_spike_multiplier=f.volatility_spike_multiplier,
        spread_filter_enabled=f.spread_enabled,
        max_spread_points=f.max_spread_points,
        economic_filter_enabled=(
            f.economic_enabled and economic_provider is not None
        ),
        economic_provider=economic_provider,
        economic_lookahead_minutes=f.economic_lookahead_minutes,
        economic_lookback_minutes=f.economic_lookback_minutes,
        economic_importance_min=f.economic_importance_min,
    )
    return BacktestFilterSimulator(filter_config)


def load_economic_provider(engine: "BacktestEngine") -> Any:
    """从 DB 加载回测期间的经济事件，构建 BacktestTradeGuardProvider。"""
    try:
        from .economic import BacktestTradeGuardProvider, _SimpleSettings
        from src.calendar.economic_calendar.trade_guard import infer_symbol_context
        from src.calendar.economic_loader import load_economic_events_window
        from src.config.database import load_db_settings
        from src.persistence.db import TimescaleWriter
        from src.persistence.repositories.economic_repo import EconomicCalendarRepository

        settings = load_db_settings()
        writer = TimescaleWriter(settings, min_conn=1, max_conn=2)
        repo = EconomicCalendarRepository(writer)

        context = infer_symbol_context(engine._config.symbol)
        events = load_economic_events_window(
            economic_repo=repo,
            start_time=engine._config.start_time,
            end_time=engine._config.end_time,
            currencies=context["currencies"] or None,
            importance_min=engine._config.filters.economic_importance_min,
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
