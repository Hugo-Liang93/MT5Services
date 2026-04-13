from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.params import Param

from src.api.deps import get_trade_trace_read_model
from src.api.schemas import ApiResponse
from src.readmodels.trade_trace import TradingFlowTraceReadModel

from .view_models import TradeTraceListItemView, TradeTraceView

router = APIRouter(tags=["trade"])


def _resolve_param(value: object) -> object:
    if isinstance(value, Param):
        default = value.default
        return None if default is ... else default
    return value


def _normalize_optional_datetime(value: datetime | None) -> datetime | None:
    value = _resolve_param(value)
    if not isinstance(value, datetime):
        return None
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _normalize_optional_string(value: object) -> str | None:
    value = _resolve_param(value)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_int(value: object, *, default: int) -> int:
    value = _resolve_param(value)
    if value is None:
        return default
    return int(value)


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


@router.get("/trade/traces", response_model=ApiResponse[list[TradeTraceListItemView]])
def trade_traces(
    trace_id: str | None = Query(default=None),
    signal_id: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    timeframe: str | None = Query(default=None),
    strategy: str | None = Query(default=None),
    status: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
    sort: str = Query(
        default="last_event_at_desc",
        pattern="^(last_event_at_desc|last_event_at_asc|started_at_desc|started_at_asc|asc|desc)$",
    ),
    trace_views: TradingFlowTraceReadModel = Depends(get_trade_trace_read_model),
) -> ApiResponse[list[TradeTraceListItemView]]:
    trace_id = _normalize_optional_string(trace_id)
    signal_id = _normalize_optional_string(signal_id)
    symbol = _normalize_optional_string(symbol)
    timeframe = _normalize_optional_string(timeframe)
    strategy = _normalize_optional_string(strategy)
    status = _normalize_optional_string(status)
    page = _normalize_int(page, default=1)
    page_size = _normalize_int(page_size, default=100)
    sort = _normalize_optional_string(sort) or "last_event_at_desc"
    normalized_from_time = _normalize_optional_datetime(from_time)
    normalized_to_time = _normalize_optional_datetime(to_time)
    result = trace_views.list_traces(
        trace_id=trace_id,
        signal_id=signal_id,
        symbol=symbol,
        timeframe=timeframe,
        strategy=strategy,
        status=status,
        from_time=normalized_from_time,
        to_time=normalized_to_time,
        page=page,
        page_size=page_size,
        sort=sort,
    )
    items = list(result.get("items") or [])
    return ApiResponse.success_response(
        data=[TradeTraceListItemView(**item) for item in items],
        metadata={
            "operation": "trade_traces",
            "trace_id": trace_id,
            "signal_id": signal_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "strategy": strategy,
            "status": status,
            "page": int(result.get("page") or page),
            "page_size": int(result.get("page_size") or page_size),
            "count": len(items),
            "total": int(result.get("total") or 0),
            "sort": sort,
            "from": normalized_from_time.isoformat() if normalized_from_time else None,
            "to": normalized_to_time.isoformat() if normalized_to_time else None,
        },
    )
