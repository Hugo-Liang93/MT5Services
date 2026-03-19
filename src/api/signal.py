from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.deps import (
    get_calibrator,
    get_htf_cache,
    get_outcome_tracker,
    get_position_manager,
    get_signal_runtime,
    get_signal_service,
)
from src.signals.evaluation.calibrator import ConfidenceCalibrator
from src.signals.evaluation.regime import MarketRegimeDetector
from src.signals.strategies.composite import CompositeSignalStrategy
from src.signals.strategies.htf_cache import HTFStateCache
from src.signals.tracking.outcome_tracker import OutcomeTracker
from src.signals.tracking.position_manager import PositionManager
from src.api.schemas import (
    ApiResponse,
    SignalDecisionModel,
    SignalEvaluateRequest,
    SignalEventModel,
    SignalSummaryModel,
)
from src.signals.runtime import SignalRuntime
from src.signals.service import SignalModule

router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("/strategies", response_model=ApiResponse[list[str]])
def list_signal_strategies(
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[list[str]]:
    strategies = service.list_strategies()
    return ApiResponse.success_response(
        data=strategies,
        metadata={"count": len(strategies)},
    )


@router.post("/evaluate", response_model=ApiResponse[SignalDecisionModel])
def evaluate_signal(
    request: SignalEvaluateRequest,
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[SignalDecisionModel]:
    decision = service.evaluate(
        symbol=request.symbol,
        timeframe=request.timeframe,
        strategy=request.strategy,
        indicators=request.indicators or None,
        metadata=request.metadata,
    )
    return ApiResponse.success_response(
        data=SignalDecisionModel(**decision.to_dict()),
        metadata={
            "symbol": request.symbol,
            "timeframe": request.timeframe,
            "strategy": request.strategy,
            "persisted": True,
        },
    )


@router.get("/recent", response_model=ApiResponse[list[SignalEventModel]])
def recent_signals(
    symbol: Optional[str] = Query(default=None),
    timeframe: Optional[str] = Query(default=None),
    strategy: Optional[str] = Query(default=None),
    action: Optional[str] = Query(default=None),
    scope: str = Query(default="confirmed", pattern="^(confirmed|preview|all)$"),
    limit: int = Query(default=200, ge=1, le=2000),
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[list[SignalEventModel]]:
    rows = service.recent_signals(
        symbol=symbol,
        timeframe=timeframe,
        strategy=strategy,
        action=action,
        scope=scope,
        limit=limit,
    )
    return ApiResponse.success_response(
        data=[SignalEventModel(**row) for row in rows],
        metadata={"count": len(rows), "scope": scope},
    )


@router.get("/summary", response_model=ApiResponse[list[SignalSummaryModel]])
def signal_summary(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    scope: str = Query(default="confirmed", pattern="^(confirmed|preview|all)$"),
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[list[SignalSummaryModel]]:
    rows = service.summary(hours=hours, scope=scope)
    return ApiResponse.success_response(
        data=[SignalSummaryModel(**row) for row in rows],
        metadata={"hours": hours, "count": len(rows), "scope": scope},
    )


@router.get(
    "/diagnostics/strategy-conflicts", response_model=ApiResponse[Dict[str, Any]]
)
def strategy_conflict_diagnostics(
    symbol: Optional[str] = Query(default=None),
    timeframe: Optional[str] = Query(default=None),
    scope: str = Query(default="confirmed", pattern="^(confirmed|preview|all)$"),
    limit: int = Query(default=2000, ge=100, le=5000),
    conflict_warn_threshold: float = Query(default=0.35, ge=0.0, le=1.0),
    hold_warn_threshold: float = Query(default=0.75, ge=0.0, le=1.0),
    confidence_warn_threshold: float = Query(default=0.45, ge=0.0, le=1.0),
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[Dict[str, Any]]:
    """诊断策略冲突、持仓倾向和指标缺失问题。"""
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


@router.get("/diagnostics/daily-report", response_model=ApiResponse[Dict[str, Any]])
def signal_daily_quality_report(
    symbol: Optional[str] = Query(default=None),
    timeframe: Optional[str] = Query(default=None),
    scope: str = Query(default="confirmed", pattern="^(confirmed|preview|all)$"),
    limit: int = Query(default=5000, ge=100, le=10000),
    conflict_warn_threshold: float = Query(default=0.35, ge=0.0, le=1.0),
    hold_warn_threshold: float = Query(default=0.75, ge=0.0, le=1.0),
    confidence_warn_threshold: float = Query(default=0.45, ge=0.0, le=1.0),
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[Dict[str, Any]]:
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


@router.get(
    "/diagnostics/aggregate-summary", response_model=ApiResponse[Dict[str, Any]]
)
def signal_diagnostics_aggregate_summary(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    scope: str = Query(default="confirmed", pattern="^(confirmed|preview|all)$"),
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[Dict[str, Any]]:
    report = service.diagnostics_aggregate_summary(hours=hours, scope=scope)
    return ApiResponse.success_response(data=report)


@router.get(
    "/diagnostics/trace/{trace_id}", response_model=ApiResponse[list[SignalEventModel]]
)
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


@router.get("/runtime/status", response_model=ApiResponse[dict])
def signal_runtime_status(
    service: SignalRuntime = Depends(get_signal_runtime),
) -> ApiResponse[dict]:
    return ApiResponse.success_response(data=service.status())


@router.get("/positions", response_model=ApiResponse[list])
def get_tracked_positions(
    manager: PositionManager = Depends(get_position_manager),
) -> ApiResponse[list]:
    """Return positions currently tracked by the signal position manager."""
    positions = manager.active_positions()
    return ApiResponse.success_response(
        data=positions,
        metadata={"count": len(positions), **manager.status()},
    )


# ── 监控端点 ─────────────────────────────────────────────────────────────────


@router.get("/regime/{symbol}/{timeframe}", response_model=ApiResponse[Dict[str, Any]])
def get_regime(
    symbol: str,
    timeframe: str,
    service: SignalModule = Depends(get_signal_service),
    runtime: SignalRuntime = Depends(get_signal_runtime),
) -> ApiResponse[Dict[str, Any]]:
    """返回指定品种/时间框架的当前 Regime 及稳定性信息。"""
    indicators = service.indicator_source.get_all_indicators(symbol, timeframe)
    detector = MarketRegimeDetector()
    detail = detector.detect_with_detail(indicators)
    stability = runtime.get_regime_stability(symbol, timeframe)
    return ApiResponse.success_response(
        data={
            **detail,
            "symbol": symbol,
            "timeframe": timeframe,
            "stability": stability,
        }
    )


@router.get("/consensus/recent", response_model=ApiResponse[list[SignalEventModel]])
def recent_consensus_signals(
    symbol: Optional[str] = Query(default=None),
    timeframe: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[list[SignalEventModel]]:
    """返回最近的 consensus 综合信号记录。"""
    rows = service.recent_signals(
        symbol=symbol,
        timeframe=timeframe,
        strategy="consensus",
        scope="confirmed",
        limit=limit,
    )
    return ApiResponse.success_response(
        data=[SignalEventModel(**row) for row in rows],
        metadata={"count": len(rows)},
    )


@router.get("/voting/stats", response_model=ApiResponse[Dict[str, Any]])
def voting_stats(
    runtime: SignalRuntime = Depends(get_signal_runtime),
) -> ApiResponse[Dict[str, Any]]:
    """返回表决引擎配置及各交易对 Regime 稳定性状态。"""
    voting_info = runtime.get_voting_info()
    regime_stability = runtime.get_regime_stability_map()
    return ApiResponse.success_response(
        data={
            "voting_enabled": voting_info.get("voting_enabled", False),
            "voting_config": voting_info.get("voting_config"),
            "regime_stability": regime_stability,
        }
    )


@router.get("/outcomes/winrate", response_model=ApiResponse[list[Dict[str, Any]]])
def signal_outcomes_winrate(
    hours: int = Query(default=168, ge=1, le=24 * 90),
    symbol: Optional[str] = Query(default=None),
    outcome_tracker: OutcomeTracker = Depends(get_outcome_tracker),
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[list[Dict[str, Any]]]:
    """从数据库查询各策略历史胜率，并附带内存实时统计。"""
    rows: list[Dict[str, Any]] = []
    try:
        repo = service.repository
        if repo is not None and hasattr(repo, "_db"):
            db_rows = repo._db.fetch_winrates(hours=hours, symbol=symbol)
            rows = [
                {
                    "strategy": r[0],
                    "action": r[1],
                    "total": r[2],
                    "wins": r[3],
                    "win_rate": float(r[4]) if r[4] is not None else None,
                    "avg_confidence": float(r[5]) if r[5] is not None else None,
                    "avg_move": float(r[6]) if r[6] is not None else None,
                }
                for r in db_rows
            ]
    except Exception:
        pass
    return ApiResponse.success_response(
        data=rows,
        metadata={
            "hours": hours,
            "symbol": symbol,
            "count": len(rows),
            "live_stats": outcome_tracker.winrate_summary(),
        },
    )


@router.get("/htf/cache", response_model=ApiResponse[Dict[str, Any]])
def htf_cache_status(
    htf_cache: HTFStateCache = Depends(get_htf_cache),
) -> ApiResponse[Dict[str, Any]]:
    """返回高时间框架方向缓存内容（用于 MTF 策略调试）。"""
    return ApiResponse.success_response(data=htf_cache.describe())


@router.get("/calibrator/status", response_model=ApiResponse[Dict[str, Any]])
def calibrator_status(
    calibrator: ConfidenceCalibrator = Depends(get_calibrator),
) -> ApiResponse[Dict[str, Any]]:
    """返回置信度校准器的当前状态（缓存大小、校准次数、胜率来源等）。"""
    return ApiResponse.success_response(data=calibrator.describe())


@router.post("/calibrator/refresh", response_model=ApiResponse[Dict[str, Any]])
def calibrator_refresh(
    hours: int = Query(default=168, ge=24, le=24 * 90),
    calibrator: ConfidenceCalibrator = Depends(get_calibrator),
) -> ApiResponse[Dict[str, Any]]:
    """立即刷新置信度校准器的历史胜率缓存。

    正常情况下校准器每小时自动刷新一次；此端点可手动强制更新，
    例如在导入历史回测数据后立即让新数据生效。
    """
    calibrator._refresh_hours = hours
    count = calibrator.refresh()
    return ApiResponse.success_response(
        data={**calibrator.describe(), "rows_loaded": count},
        metadata={"hours": hours},
    )


@router.get("/strategies/composite", response_model=ApiResponse[list[Dict[str, Any]]])
def list_composite_strategies(
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[list[Dict[str, Any]]]:
    """返回所有复合策略的描述信息（子策略列表、组合模式、Regime 亲和度）。"""
    result = []
    for name in service.list_strategies():
        impl = service._strategies.get(name)
        if isinstance(impl, CompositeSignalStrategy):
            result.append(impl.describe())
    return ApiResponse.success_response(
        data=result,
        metadata={"count": len(result)},
    )
