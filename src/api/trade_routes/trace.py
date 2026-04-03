from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import get_trade_trace_read_model
from src.api.schemas import ApiResponse
from src.readmodels.trade_trace import TradingFlowTraceReadModel

from .view_models import TradeTraceView

router = APIRouter(tags=["trade"])


@router.get("/trade/trace/{signal_id}", response_model=ApiResponse[TradeTraceView])
def trade_trace_by_signal_id(
    signal_id: str,
    trace_views: TradingFlowTraceReadModel = Depends(get_trade_trace_read_model),
) -> ApiResponse[TradeTraceView]:
    return ApiResponse.success_response(
        data=trace_views.trace_by_signal_id(signal_id),
        metadata={
            "operation": "trade_trace_by_signal_id",
            "signal_id": signal_id,
        },
    )


@router.get("/trade/trace/by-trace/{trace_id}", response_model=ApiResponse[TradeTraceView])
def trade_trace_by_trace_id(
    trace_id: str,
    trace_views: TradingFlowTraceReadModel = Depends(get_trade_trace_read_model),
) -> ApiResponse[TradeTraceView]:
    return ApiResponse.success_response(
        data=trace_views.trace_by_trace_id(trace_id),
        metadata={
            "operation": "trade_trace_by_trace_id",
            "trace_id": trace_id,
        },
    )
