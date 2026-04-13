from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from src.api.deps import get_runtime_read_model
from src.api.schemas import ApiResponse
from src.readmodels.runtime import RuntimeReadModel

from ..common import normalize_optional_status
from ..view_models import (
    ExecutionContextListView,
    PendingOrderStateListView,
    PositionRuntimeStateListView,
)

router = APIRouter(tags=["trade"])


@router.get(
    "/trade/state/pending/active",
    response_model=ApiResponse[PendingOrderStateListView],
)
def trade_active_pending_state_list(
    limit: int = Query(default=50, ge=1, le=500),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[PendingOrderStateListView]:
    return ApiResponse.success_response(
        data=runtime_views.active_pending_order_payload(limit=limit),
        metadata={
            "operation": "trade_active_pending_state_list",
            "limit": limit,
        },
    )


@router.get(
    "/trade/state/execution-contexts",
    response_model=ApiResponse[ExecutionContextListView],
)
def trade_pending_execution_context_list(
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[ExecutionContextListView]:
    return ApiResponse.success_response(
        data=runtime_views.pending_execution_context_payload(),
        metadata={"operation": "trade_pending_execution_context_list"},
    )


@router.get(
    "/trade/state/pending/lifecycle",
    response_model=ApiResponse[PendingOrderStateListView],
)
def trade_pending_lifecycle_state_list(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[PendingOrderStateListView]:
    normalized_status = normalize_optional_status(status)
    statuses = [normalized_status] if normalized_status else None
    return ApiResponse.success_response(
        data=runtime_views.pending_order_state_payload(
            statuses=statuses,
            limit=limit,
        ),
        metadata={
            "operation": "trade_pending_lifecycle_state_list",
            "status": normalized_status,
            "limit": limit,
        },
    )


@router.get(
    "/trade/state/positions",
    response_model=ApiResponse[PositionRuntimeStateListView],
)
def trade_position_state_list(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[PositionRuntimeStateListView]:
    normalized_status = normalize_optional_status(status)
    statuses = [normalized_status] if normalized_status else None
    return ApiResponse.success_response(
        data=runtime_views.position_runtime_state_payload(
            statuses=statuses,
            limit=limit,
        ),
        metadata={
            "operation": "trade_position_state_list",
            "status": normalized_status,
            "limit": limit,
        },
    )
