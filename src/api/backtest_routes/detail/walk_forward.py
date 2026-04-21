"""GET /v1/backtest/history/{run_id}/walk-forward — Walk-Forward 分窗口结果（P11 Phase 2）。

解析规则：路径中的 `{run_id}` 先作为 wf_run_id 查；若不存在再作为 backtest_run_id
查最新一次 WF。这覆盖前端两种常见调用：
- 拿到 wf_run_id（POST /walk-forward 返回或 lab_impact 列出）直接查 detail
- 拿到 backtest_runs.run_id 想看其关联的 WF（如有）
"""

from __future__ import annotations

import logging
from typing import cast

from fastapi import APIRouter, Depends

from src.api import deps
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import ApiResponse
from src.readmodels.walk_forward_read import WalkForwardReadModel

from .schemas import WalkForwardDetail

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/history/{run_id}/walk-forward",
    response_model=ApiResponse[WalkForwardDetail],
)
def get_walk_forward_detail(
    run_id: str,
    read_model: WalkForwardReadModel = Depends(deps.get_walk_forward_read_model),
) -> ApiResponse[WalkForwardDetail]:
    try:
        payload = read_model.build_by_wf_run_id(run_id)
        if payload is None:
            payload = read_model.build_by_backtest_run_id(run_id)
    except Exception as exc:
        logger.exception("walk-forward detail build failed for %s", run_id)
        return cast(
            ApiResponse[WalkForwardDetail],
            ApiResponse.error_response(
                error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
                error_message=str(exc),
                suggested_action=AIErrorAction.CONTACT_SUPPORT,
            ),
        )
    if payload is None:
        return cast(
            ApiResponse[WalkForwardDetail],
            ApiResponse.error_response(
                error_code=AIErrorCode.NOT_FOUND,
                error_message=(f"Walk-forward result not found for run_id: {run_id}"),
                suggested_action=AIErrorAction.CONTACT_SUPPORT,
                details={"run_id": run_id},
            ),
        )
    return ApiResponse.success_response(WalkForwardDetail.model_validate(payload))
