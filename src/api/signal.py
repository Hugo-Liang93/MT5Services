from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from src.api.deps import get_signal_runtime, get_signal_service
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
    limit: int = Query(default=200, ge=1, le=2000),
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[list[SignalEventModel]]:
    rows = service.recent_signals(
        symbol=symbol,
        timeframe=timeframe,
        strategy=strategy,
        action=action,
        limit=limit,
    )
    return ApiResponse.success_response(
        data=[SignalEventModel(**row) for row in rows],
        metadata={"count": len(rows)},
    )


@router.get("/summary", response_model=ApiResponse[list[SignalSummaryModel]])
def signal_summary(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[list[SignalSummaryModel]]:
    rows = service.summary(hours=hours)
    return ApiResponse.success_response(
        data=[SignalSummaryModel(**row) for row in rows],
        metadata={"hours": hours, "count": len(rows)},
    )


@router.get("/runtime/status", response_model=ApiResponse[dict])
def signal_runtime_status(service: SignalRuntime = Depends(get_signal_runtime)) -> ApiResponse[dict]:
    return ApiResponse.success_response(data=service.status())

