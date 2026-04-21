"""GET /v1/backtest/history/{run_id}/trade-structure — 交易结构统计（P11 Phase 4a）。

派生自 `backtest_trades`：avg_hold / max_loss_streak / MFE-MAE 分布 / holding buckets。
历史无 mfe/mae/hold 数据的 run 会标 `partial_data=True`，前端按 null 渲染。
"""

from __future__ import annotations

import logging
from typing import cast

from fastapi import APIRouter, Depends

from src.api import deps
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import ApiResponse
from src.readmodels.backtest_detail import BacktestDetailReadModel

from .schemas import TradeStructure

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/history/{run_id}/trade-structure",
    response_model=ApiResponse[TradeStructure],
)
def get_trade_structure(
    run_id: str,
    read_model: BacktestDetailReadModel = Depends(deps.get_backtest_detail_read_model),
) -> ApiResponse[TradeStructure]:
    try:
        payload = read_model.build_trade_structure(run_id)
    except Exception as exc:
        logger.exception("trade-structure build failed for %s", run_id)
        return cast(
            ApiResponse[TradeStructure],
            ApiResponse.error_response(
                error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
                error_message=str(exc),
                suggested_action=AIErrorAction.CONTACT_SUPPORT,
            ),
        )
    if payload is None:
        return cast(
            ApiResponse[TradeStructure],
            ApiResponse.error_response(
                error_code=AIErrorCode.NOT_FOUND,
                error_message=f"Backtest run not found: {run_id}",
                suggested_action=AIErrorAction.CONTACT_SUPPORT,
                details={"run_id": run_id},
            ),
        )
    return ApiResponse.success_response(TradeStructure.model_validate(payload))
