from __future__ import annotations

from typing import Dict, Optional

from fastapi import APIRouter, Depends, Query

from src.api.deps import get_signal_quality_tracker, get_signal_runtime, get_signal_service, get_trade_outcome_tracker
from src.api.schemas import ApiResponse, SignalEventModel
from src.signals.orchestration import SignalRuntime
from src.signals.service import SignalModule
from src.trading.tracking import SignalQualityTracker, TradeOutcomeTracker

router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("/diagnostics/strategy-conflicts", response_model=ApiResponse[Dict[str, object]])
def strategy_conflict_diagnostics(
    symbol: Optional[str] = Query(default=None),
    timeframe: Optional[str] = Query(default=None),
    scope: str = Query(default="confirmed", pattern="^(confirmed|preview|all)$"),
    limit: int = Query(default=2000, ge=100, le=5000),
    conflict_warn_threshold: float = Query(default=0.35, ge=0.0, le=1.0),
    hold_warn_threshold: float = Query(default=0.75, ge=0.0, le=1.0),
    confidence_warn_threshold: float = Query(default=0.45, ge=0.0, le=1.0),
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[Dict[str, object]]:
    report = service.strategy_diagnostics(
        symbol=symbol,
        timeframe=timeframe,
        scope=scope,
        limit=limit,
        conflict_warn_threshold=conflict_warn_threshold,
        hold_warn_threshold=hold_warn_threshold,
        confidence_warn_threshold=confidence_warn_threshold,
    )
    return ApiResponse.success_response(data=report)


@router.get("/diagnostics/daily-report", response_model=ApiResponse[Dict[str, object]])
def signal_daily_quality_report(
    symbol: Optional[str] = Query(default=None),
    timeframe: Optional[str] = Query(default=None),
    scope: str = Query(default="confirmed", pattern="^(confirmed|preview|all)$"),
    limit: int = Query(default=5000, ge=100, le=10000),
    conflict_warn_threshold: float = Query(default=0.35, ge=0.0, le=1.0),
    hold_warn_threshold: float = Query(default=0.75, ge=0.0, le=1.0),
    confidence_warn_threshold: float = Query(default=0.45, ge=0.0, le=1.0),
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[Dict[str, object]]:
    report = service.daily_quality_report(
        symbol=symbol,
        timeframe=timeframe,
        scope=scope,
        limit=limit,
        conflict_warn_threshold=conflict_warn_threshold,
        hold_warn_threshold=hold_warn_threshold,
        confidence_warn_threshold=confidence_warn_threshold,
    )
    return ApiResponse.success_response(data=report)


@router.get("/diagnostics/aggregate-summary", response_model=ApiResponse[Dict[str, object]])
def signal_diagnostics_aggregate_summary(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    scope: str = Query(default="confirmed", pattern="^(confirmed|preview|all)$"),
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[Dict[str, object]]:
    return ApiResponse.success_response(
        data=service.diagnostics_aggregate_summary(hours=hours, scope=scope)
    )


@router.get("/monitoring/quality/{symbol}/{timeframe}", response_model=ApiResponse[Dict[str, object]])
def signal_monitoring_quality(
    symbol: str,
    timeframe: str,
    limit: int = Query(default=1000, ge=100, le=5000),
    service: SignalModule = Depends(get_signal_service),
    runtime: SignalRuntime = Depends(get_signal_runtime),
) -> ApiResponse[Dict[str, object]]:
    return ApiResponse.success_response(
        data={
            "symbol": symbol,
            "timeframe": timeframe,
            "regime": service.regime_report(
                symbol=symbol,
                timeframe=timeframe,
                runtime=runtime,
            ),
            "quality": service.daily_quality_report(
                symbol=symbol,
                timeframe=timeframe,
                scope="confirmed",
                limit=limit,
            ),
        },
        metadata={"symbol": symbol, "timeframe": timeframe, "limit": limit},
    )


@router.get("/diagnostics/trace/{trace_id}", response_model=ApiResponse[list[SignalEventModel]])
def signal_trace_events(
    trace_id: str,
    scope: str = Query(default="all", pattern="^(confirmed|preview|all)$"),
    limit: int = Query(default=2000, ge=1, le=5000),
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[list[SignalEventModel]]:
    rows = service.recent_by_trace_id(trace_id=trace_id, scope=scope, limit=limit)
    return ApiResponse.success_response(
        data=[SignalEventModel(**row) for row in rows],
        metadata={"trace_id": trace_id, "count": len(rows)},
    )


@router.get("/outcomes/winrate", response_model=ApiResponse[list[Dict[str, object]]])
def signal_outcomes_winrate(
    hours: int = Query(default=168, ge=1, le=24 * 90),
    symbol: Optional[str] = Query(default=None),
    quality_tracker: SignalQualityTracker = Depends(get_signal_quality_tracker),
    trade_tracker: TradeOutcomeTracker = Depends(get_trade_outcome_tracker),
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[list[Dict[str, object]]]:
    rows = service.strategy_winrates(hours=hours, symbol=symbol)
    return ApiResponse.success_response(
        data=rows,
        metadata={
            "hours": hours,
            "symbol": symbol,
            "count": len(rows),
            "signal_quality_stats": quality_tracker.winrate_summary(),
            "trade_outcome_stats": trade_tracker.summary(),
        },
    )
