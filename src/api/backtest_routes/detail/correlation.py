"""GET /v1/backtest/history/{run_id}/correlation-analysis — 最新一次 correlation detail（P11 Phase 3）。

解析规则：`{run_id}` 默认作为 `backtest_run_id` 查最新；
若 `?analysis_id=xxx` 指定则按该 ID 精确查。
"""

from __future__ import annotations

import logging
from typing import Optional, cast

from fastapi import APIRouter, Depends, Query

from src.api import deps
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import ApiResponse
from src.readmodels.correlation_read import CorrelationAnalysisReadModel

from .schemas import CorrelationAnalysisDetail

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/history/{run_id}/correlation-analysis",
    response_model=ApiResponse[CorrelationAnalysisDetail],
)
def get_correlation_analysis(
    run_id: str,
    analysis_id: Optional[str] = Query(
        None,
        description="指定 analysis_id 精确查询；省略则返回最新一次分析",
    ),
    read_model: CorrelationAnalysisReadModel = Depends(deps.get_correlation_read_model),
) -> ApiResponse[CorrelationAnalysisDetail]:
    try:
        if analysis_id:
            payload = read_model.build_by_analysis_id(analysis_id)
        else:
            payload = read_model.build_latest_for_run(run_id)
    except Exception as exc:
        logger.exception(
            "correlation-analysis build failed for %s (analysis_id=%s)",
            run_id,
            analysis_id,
        )
        return cast(
            ApiResponse[CorrelationAnalysisDetail],
            ApiResponse.error_response(
                error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
                error_message=str(exc),
                suggested_action=AIErrorAction.CONTACT_SUPPORT,
            ),
        )
    if payload is None:
        return cast(
            ApiResponse[CorrelationAnalysisDetail],
            ApiResponse.error_response(
                error_code=AIErrorCode.NOT_FOUND,
                error_message=(
                    f"No correlation analysis found for run_id={run_id}"
                    + (f", analysis_id={analysis_id}" if analysis_id else "")
                ),
                suggested_action=AIErrorAction.CONTACT_SUPPORT,
                details={"run_id": run_id, "analysis_id": analysis_id},
            ),
        )
    return ApiResponse.success_response(
        CorrelationAnalysisDetail.model_validate(payload)
    )
