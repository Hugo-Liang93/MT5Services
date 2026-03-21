from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from src.api.deps import (
    get_calibrator,
    get_htf_cache,
    get_market_service,
    get_market_structure_analyzer,
    get_signal_quality_tracker,
    get_trade_outcome_tracker,
    get_position_manager,
    get_signal_runtime,
    get_signal_service,
)
from src.signals.evaluation.calibrator import ConfidenceCalibrator
from src.market import MarketDataService
from src.market_structure import MarketStructureAnalyzer
from src.signals.strategies.htf_cache import HTFStateCache
from src.signals.orchestration import SignalRuntime
from src.trading.signal_quality_tracker import SignalQualityTracker
from src.trading.trade_outcome_tracker import TradeOutcomeTracker
from src.trading.position_manager import PositionManager
from src.api.schemas import (
    ApiResponse,
    SignalDecisionModel,
    SignalEvaluateRequest,
    SignalEventModel,
    SignalSummaryModel,
)
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


@router.post("/evaluate", response_model=ApiResponse[Any])
def evaluate_signal(
    request: SignalEvaluateRequest,
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[Any]:
    indicators = request.indicators or None
    if request.strategy:
        decision = service.evaluate(
            symbol=request.symbol,
            timeframe=request.timeframe,
            strategy=request.strategy,
            indicators=indicators,
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

    # strategy 未指定：评估所有策略，按置信度排序返回
    all_strategies = service.list_strategies()
    decisions: List[SignalDecisionModel] = []
    for strategy_name in all_strategies:
        try:
            d = service.evaluate(
                symbol=request.symbol,
                timeframe=request.timeframe,
                strategy=strategy_name,
                indicators=indicators,
                metadata=request.metadata,
            )
            decisions.append(SignalDecisionModel(**d.to_dict()))
        except Exception:
            continue

    # 优先返回非 hold 且置信度最高的；全 hold 时返回置信度最高的 hold
    actionable = [d for d in decisions if d.action in ("buy", "sell")]
    best = sorted(actionable, key=lambda x: x.confidence, reverse=True) or sorted(
        decisions, key=lambda x: x.confidence, reverse=True
    )
    return ApiResponse.success_response(
        data=best,
        metadata={
            "symbol": request.symbol,
            "timeframe": request.timeframe,
            "strategy": "all",
            "total": len(decisions),
            "actionable": len(actionable),
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


@router.get("/best", response_model=ApiResponse[List[Dict[str, Any]]])
def best_signals_per_timeframe(
    symbol: Optional[str] = Query(default=None),
    hours: int = Query(default=4, ge=1, le=24 * 7),
    min_confidence: float = Query(default=0.3, ge=0.0, le=1.0),
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[List[Dict[str, Any]]]:
    """返回每个 (symbol, timeframe) 组合中置信度最高的可执行信号（buy/sell）。

    适用场景：快速概览当前哪个时间框架有明确方向性信号，避免遍历全量 summary。
    """
    rows = service.recent_signals(
        symbol=symbol,
        action=None,
        scope="confirmed",
        limit=2000,
    )
    from datetime import timedelta

    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        action = row.get("action", "")
        if action not in ("buy", "sell"):
            continue
        confidence = row.get("confidence", 0.0) or 0.0
        if confidence < min_confidence:
            continue
        ts_raw = row.get("timestamp") or row.get("created_at") or ""
        try:
            ts = datetime.fromisoformat(str(ts_raw))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                continue
        except (ValueError, TypeError):
            continue
        key = f"{row.get('symbol')}/{row.get('timeframe')}"
        existing = buckets.get(key)
        if existing is None or confidence > (existing.get("confidence") or 0.0):
            buckets[key] = row
    result = sorted(buckets.values(), key=lambda x: x.get("confidence", 0.0), reverse=True)
    return ApiResponse.success_response(
        data=result,
        metadata={
            "symbol": symbol,
            "hours": hours,
            "min_confidence": min_confidence,
            "count": len(result),
        },
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
    "/monitoring/quality/{symbol}/{timeframe}",
    response_model=ApiResponse[Dict[str, Any]],
)
def signal_monitoring_quality(
    symbol: str,
    timeframe: str,
    limit: int = Query(default=1000, ge=100, le=5000),
    service: SignalModule = Depends(get_signal_service),
    runtime: SignalRuntime = Depends(get_signal_runtime),
) -> ApiResponse[Dict[str, Any]]:
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
    return ApiResponse.success_response(
        data=service.regime_report(
            symbol=symbol,
            timeframe=timeframe,
            runtime=runtime,
        )
    )


@router.get(
    "/market-structure/{symbol}/{timeframe}",
    response_model=ApiResponse[Dict[str, Any]],
)
def get_market_structure(
    symbol: str,
    timeframe: str,
    analyzer: MarketStructureAnalyzer = Depends(get_market_structure_analyzer),
    market_service: MarketDataService = Depends(get_market_service),
) -> ApiResponse[Dict[str, Any]]:
    event_time = datetime.now(timezone.utc)
    latest_close: float | None = None
    price_source = "latest_closed_bar"
    try:
        quote = market_service.get_quote(symbol)
    except Exception:
        quote = None
    if quote is not None:
        raw_last = getattr(quote, "last", None)
        try:
            latest_close = float(raw_last) if raw_last is not None else None
        except (TypeError, ValueError):
            latest_close = None
        if latest_close is not None and latest_close > 0:
            price_source = "live_quote_last"
        else:
            try:
                bid = float(getattr(quote, "bid", 0.0) or 0.0)
                ask = float(getattr(quote, "ask", 0.0) or 0.0)
            except (TypeError, ValueError):
                bid = 0.0
                ask = 0.0
            if bid > 0 and ask > 0:
                latest_close = (bid + ask) / 2.0
                price_source = "live_quote_mid"
    return ApiResponse.success_response(
        data=analyzer.analyze(
            symbol,
            timeframe,
            event_time=event_time,
            latest_close=latest_close,
        ),
        metadata={
            "symbol": symbol,
            "timeframe": timeframe,
            "analysis_mode": "live_quote" if latest_close is not None else "closed_bar_fallback",
            "price_source": price_source,
            "event_time": event_time.isoformat(),
        },
    )


@router.get("/consensus/recent", response_model=ApiResponse[list[SignalEventModel]])
def recent_consensus_signals(
    symbol: Optional[str] = Query(default=None),
    timeframe: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[list[SignalEventModel]]:
    """返回最近的 consensus 综合信号记录。"""
    rows = service.recent_consensus_signals(
        symbol=symbol,
        timeframe=timeframe,
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
    quality_tracker: SignalQualityTracker = Depends(get_signal_quality_tracker),
    trade_tracker: TradeOutcomeTracker = Depends(get_trade_outcome_tracker),
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[list[Dict[str, Any]]]:
    """从数据库查询各策略历史胜率，并附带内存实时统计。"""
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
    result = service.list_composite_strategies()
    return ApiResponse.success_response(
        data=result,
        metadata={"count": len(result)},
    )
