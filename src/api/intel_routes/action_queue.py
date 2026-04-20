"""P10.2: /v1/intel/action-queue canonical 行动队列。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from src.api.deps import get_intel_read_model
from src.api.schemas import ApiResponse
from src.readmodels.intel import IntelReadModel

router = APIRouter(tags=["intel"])


@router.get(
    "/intel/action-queue",
    response_model=ApiResponse[dict],
    summary="Intel guard-aware 行动队列（P10.2）",
    description=(
        "替代 QuantX `Intel` 页本地对 `signals/recent + market/context` 的"
        "启发式排序。被 `close_only / quote_stale / circuit_open / worker_not_ready / "
        "block_new_trades` 阻断的机会不会进入可行动队列。`priority` 与 `rank_source` "
        "由后端派生，前端只负责展示。`account_candidates[]` 从 "
        "`signal_config.account_bindings` 反向索引，给出每个 signal 可执行账户列表。"
    ),
)
def intel_action_queue(
    symbol: Optional[str] = Query(default=None),
    timeframe: Optional[str] = Query(default=None),
    strategy: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=50),
    read_model: IntelReadModel = Depends(get_intel_read_model),
) -> ApiResponse[dict]:
    payload = read_model.build_action_queue(
        symbol=symbol,
        timeframe=timeframe,
        strategy=strategy,
        page=page,
        page_size=page_size,
    )
    return ApiResponse.success_response(
        data=payload,
        metadata={
            "operation": "intel_action_queue",
            "symbol": symbol,
            "timeframe": timeframe,
            "strategy": strategy,
            "page": payload["pagination"]["page"],
            "page_size": payload["pagination"]["page_size"],
            "total": payload["pagination"]["total"],
            "entry_count": len(payload["entries"]),
        },
    )
