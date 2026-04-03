from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from src.api.deps import get_signal_service
from src.api.schemas import ApiResponse, SignalDecisionModel, SignalEvaluateRequest, SignalEventModel, SignalSummaryModel
from src.signals.service import SignalModule

router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("/strategies", response_model=ApiResponse[list[Dict[str, Any]]])
def list_signal_strategies(
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[list[Dict[str, Any]]]:
    strategies = service.strategy_catalog()
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
    actionable = [d for d in decisions if d.direction in ("buy", "sell")]
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
        direction=action,
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
    rows = service.recent_signals(
        symbol=symbol,
        direction=None,
        scope="confirmed",
        limit=2000,
    )
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        action = row.get("direction", "")
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


@router.get("/consensus/recent", response_model=ApiResponse[list[SignalEventModel]])
def recent_consensus_signals(
    symbol: Optional[str] = Query(default=None),
    timeframe: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[list[SignalEventModel]]:
    rows = service.recent_consensus_signals(
        symbol=symbol,
        timeframe=timeframe,
        limit=limit,
    )
    return ApiResponse.success_response(
        data=[SignalEventModel(**row) for row in rows],
        metadata={"count": len(rows)},
    )


@router.get("/strategies/composite", response_model=ApiResponse[list[Dict[str, Any]]])
def list_composite_strategies(
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[list[Dict[str, Any]]]:
    result = service.list_composite_strategies()
    return ApiResponse.success_response(
        data=result,
        metadata={"count": len(result)},
    )
