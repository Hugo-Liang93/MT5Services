from __future__ import annotations

from fastapi import APIRouter

from .signal_routes import catalog_router, diagnostics_router, runtime_router
from .signal_routes.catalog import (
    best_signals_per_timeframe,
    evaluate_signal,
    list_signal_strategies,
    recent_consensus_signals,
    recent_signals,
    signal_summary,
)
from .signal_routes.diagnostics import (
    signal_daily_quality_report,
    signal_diagnostics_aggregate_summary,
    signal_monitoring_quality,
    signal_outcomes_winrate,
    signal_trace_events,
    strategy_conflict_diagnostics,
)
from .signal_routes.runtime import (
    calibrator_refresh,
    calibrator_status,
    get_market_structure,
    get_regime,
    get_tracked_positions,
    htf_cache_status,
    signal_runtime_status,
    voting_stats,
)

router = APIRouter(tags=["signals"])
router.include_router(catalog_router)
router.include_router(diagnostics_router)
router.include_router(runtime_router)

__all__ = [
    "best_signals_per_timeframe",
    "calibrator_refresh",
    "calibrator_status",
    "evaluate_signal",
    "get_market_structure",
    "get_regime",
    "get_tracked_positions",
    "htf_cache_status",
    "list_signal_strategies",
    "recent_consensus_signals",
    "recent_signals",
    "router",
    "signal_daily_quality_report",
    "signal_diagnostics_aggregate_summary",
    "signal_monitoring_quality",
    "signal_outcomes_winrate",
    "signal_runtime_status",
    "signal_summary",
    "signal_trace_events",
    "strategy_conflict_diagnostics",
    "voting_stats",
]
