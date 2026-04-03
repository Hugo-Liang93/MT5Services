from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query

from src.api import deps
from src.api.admin_schemas import StrategyDetail, StrategyPerformanceReport
from src.api.schemas import ApiResponse
from src.signals.evaluation.calibrator import ConfidenceCalibrator
from src.signals.evaluation.performance import StrategyPerformanceTracker
from src.signals.orchestration import SignalRuntime
from src.signals.service import SignalModule

from .common import build_strategy_detail

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/performance/strategies", response_model=ApiResponse[StrategyPerformanceReport])
def admin_performance_strategies(
    hours: int = Query(default=168, ge=1, le=720, description="历史统计范围，单位小时。"),
    perf_tracker: StrategyPerformanceTracker = Depends(deps.get_performance_tracker),
    calibrator: ConfidenceCalibrator = Depends(deps.get_calibrator),
    signal_svc: SignalModule = Depends(deps.get_signal_service),
) -> ApiResponse[StrategyPerformanceReport]:
    try:
        ranking = perf_tracker.strategy_ranking()
    except Exception:
        ranking = []
    try:
        summary = perf_tracker.describe()
    except Exception:
        summary = {}
    try:
        winrates = signal_svc.strategy_winrates(hours=hours)
    except Exception:
        winrates = []
    try:
        calibrator_info = calibrator.describe()
    except Exception:
        calibrator_info = {}
    report = StrategyPerformanceReport(session_ranking=ranking, session_summary=summary, historical_winrates=winrates, calibrator=calibrator_info)
    return ApiResponse.success_response(report)


@router.get("/performance/confidence-pipeline/{symbol}/{timeframe}", response_model=ApiResponse[Dict[str, Any]])
def admin_confidence_pipeline(
    symbol: str,
    timeframe: str,
    signal_runtime: SignalRuntime = Depends(deps.get_signal_runtime),
    signal_svc: SignalModule = Depends(deps.get_signal_service),
    perf_tracker: StrategyPerformanceTracker = Depends(deps.get_performance_tracker),
    calibrator: ConfidenceCalibrator = Depends(deps.get_calibrator),
) -> ApiResponse[Dict[str, Any]]:
    regime_info = signal_runtime.get_regime_stability(symbol, timeframe) or {}
    strategies_pipeline: List[Dict[str, Any]] = []
    for descriptor in signal_svc.strategy_catalog():
        name = str(descriptor["name"])
        try:
            stats = perf_tracker.get_strategy_stats(name)
        except Exception:
            stats = None
        try:
            multiplier = perf_tracker.get_multiplier(name)
        except Exception:
            multiplier = 1.0
        strategies_pipeline.append({**descriptor, "session_multiplier": multiplier, "session_stats": stats})
    return ApiResponse.success_response(
        {"symbol": symbol, "timeframe": timeframe, "regime": regime_info, "calibrator": calibrator.describe(), "strategies": strategies_pipeline}
    )


@router.get("/strategies", response_model=ApiResponse[List[StrategyDetail]])
def admin_strategies(signal_svc: SignalModule = Depends(deps.get_signal_service)) -> ApiResponse[List[StrategyDetail]]:
    return ApiResponse.success_response([StrategyDetail(**row) for row in signal_svc.strategy_catalog()])


@router.get("/strategies/{name}", response_model=ApiResponse[Dict[str, Any]])
def admin_strategy_detail(
    name: str,
    signal_svc: SignalModule = Depends(deps.get_signal_service),
    perf_tracker: StrategyPerformanceTracker = Depends(deps.get_performance_tracker),
) -> ApiResponse[Dict[str, Any]]:
    if name not in signal_svc.list_strategies():
        return ApiResponse.error_response(error_code="VALIDATION_ERROR", error_message=f"Strategy '{name}' not found", suggested_action="Check /v1/admin/strategies for available strategies")
    detail = build_strategy_detail(name, signal_svc)
    try:
        stats = perf_tracker.get_strategy_stats(name)
    except Exception:
        stats = None
    return ApiResponse.success_response({**detail.model_dump(), "session_performance": stats})
