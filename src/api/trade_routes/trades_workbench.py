"""P10.3: /v1/trades/workbench 列表 + /v1/trades/{trade_id} 详情。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query

from src.api.deps import get_trades_workbench_read_model
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import ApiResponse
from src.readmodels.trades_workbench import TradesWorkbenchReadModel

from .state_routes.common import (
    normalize_int,
    normalize_optional_datetime,
    normalize_optional_string,
)
from .view_models import TradeDetailView, TradesWorkbenchView

router = APIRouter(tags=["trade"])


@router.get(
    "/trades/workbench",
    response_model=ApiResponse[TradesWorkbenchView],
    summary="Trades 工作台 canonical 列表（P10.3）",
    description=(
        "替代 QuantX 前端 `AuditTradesAggregate` 本地聚合。基于 `trade_outcomes` 表，"
        "返回单账户交易记录分页、统计摘要与 freshness 契约字段。"
    ),
)
def trades_workbench(
    symbol: Optional[str] = Query(default=None),
    timeframe: Optional[str] = Query(default=None),
    strategy: Optional[str] = Query(default=None),
    direction: Optional[str] = Query(default=None, pattern="^(buy|sell)$"),
    won: Optional[bool] = Query(default=None, description="filter on trade_outcomes.won"),
    from_time: Optional[datetime] = Query(default=None, alias="from"),
    to_time: Optional[datetime] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    sort: str = Query(
        default="recorded_at_desc",
        pattern="^(recorded_at_desc|recorded_at_asc|asc|desc)$",
    ),
    read_model: TradesWorkbenchReadModel = Depends(get_trades_workbench_read_model),
) -> ApiResponse[dict]:
    symbol = normalize_optional_string(symbol)
    timeframe = normalize_optional_string(timeframe)
    strategy = normalize_optional_string(strategy)
    direction = normalize_optional_string(direction)
    page = normalize_int(page, default=1)
    page_size = normalize_int(page_size, default=50)
    sort = normalize_optional_string(sort) or "recorded_at_desc"
    normalized_from_time = normalize_optional_datetime(from_time)
    normalized_to_time = normalize_optional_datetime(to_time)

    payload = read_model.build_workbench(
        symbol=symbol,
        timeframe=timeframe,
        strategy=strategy,
        direction=direction,
        won=won,
        from_time=normalized_from_time,
        to_time=normalized_to_time,
        page=page,
        page_size=page_size,
        sort=sort,
    )
    return ApiResponse.success_response(
        data=payload,
        metadata={
            "operation": "trades_workbench",
            "account_alias": payload.get("account_alias"),
            "symbol": symbol,
            "timeframe": timeframe,
            "strategy": strategy,
            "direction": direction,
            "won": won,
            "page": page,
            "page_size": page_size,
            "total": payload.get("pagination", {}).get("total", 0),
            "sort": sort,
        },
    )


@router.get(
    "/trades/{trade_id}",
    response_model=ApiResponse[TradeDetailView],
    summary="单次交易 6 维详情（P10.3）",
    description=(
        "按 `trade_id` 组装 `plan_vs_live / lifecycle / risk_review / receipts / "
        "evidence / linked_account_state`。本阶段 `trade_id` 使用 `signal_id` "
        "作为主键，贯通 signal → execution → outcome 全链路。"
    ),
)
def trade_detail(
    trade_id: str,
    read_model: TradesWorkbenchReadModel = Depends(get_trades_workbench_read_model),
) -> ApiResponse[dict]:
    normalized = normalize_optional_string(trade_id) or ""
    if not normalized:
        return ApiResponse.error_response(
            error_code=AIErrorCode.VALIDATION_ERROR,
            error_message="trade_id required",
            suggested_action=AIErrorAction.VALIDATE_PARAMETERS,
        )
    payload = read_model.build_trade_detail(trade_id=normalized)
    if payload is None:
        return ApiResponse.error_response(
            error_code=AIErrorCode.NOT_FOUND,
            error_message=f"trade_id not found: {normalized}",
            suggested_action=AIErrorAction.VALIDATE_PARAMETERS,
        )
    return ApiResponse.success_response(
        data=payload,
        metadata={
            "operation": "trade_detail",
            "trade_id": normalized,
            "signal_id": payload.get("signal_id"),
            "trace_id": payload.get("trace_id"),
        },
    )
