"""GET /v1/backtest/history/{run_id}/execution-realism — 执行现实性（P11 Phase 4a 占位）。

Phase 4a 契约先行：字段固定，但实际敏感性分析（spread/slippage/broker/session）
由 Phase 4b 的引擎重放填充。当前所有历史 run 返回 `available=False` + 全 None。
"""

from __future__ import annotations

import logging
from typing import cast

from fastapi import APIRouter, Depends

from src.api import deps
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import ApiResponse
from src.readmodels.backtest_detail import BacktestDetailReadModel

from .schemas import ExecutionRealism

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/history/{run_id}/execution-realism",
    response_model=ApiResponse[ExecutionRealism],
)
def get_execution_realism(
    run_id: str,
    read_model: BacktestDetailReadModel = Depends(deps.get_backtest_detail_read_model),
) -> ApiResponse[ExecutionRealism]:
    try:
        payload = read_model.build_execution_realism(run_id)
    except Exception as exc:
        logger.exception("execution-realism build failed for %s", run_id)
        return cast(
            ApiResponse[ExecutionRealism],
            ApiResponse.error_response(
                error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
                error_message=str(exc),
                suggested_action=AIErrorAction.CONTACT_SUPPORT,
            ),
        )
    if payload is None:
        return cast(
            ApiResponse[ExecutionRealism],
            ApiResponse.error_response(
                error_code=AIErrorCode.NOT_FOUND,
                error_message=f"Backtest run not found: {run_id}",
                suggested_action=AIErrorAction.CONTACT_SUPPORT,
                details={"run_id": run_id},
            ),
        )
    return ApiResponse.success_response(ExecutionRealism.model_validate(payload))
