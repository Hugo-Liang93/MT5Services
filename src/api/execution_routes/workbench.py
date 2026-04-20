"""单账户执行工作台 — `GET /v1/execution/workbench`（P9 Phase 1）。

聚合 9 块 read model 输出，前端用一次请求即可初始化 Execution 工作台。
跨账户聚合由 BFF 完成，本端点仅返回单账户视图。

契约：docs/design/quantx-data-freshness-tiering.md
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from src.api.deps import get_workbench_read_model
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import ApiResponse, WorkbenchPayload
from src.readmodels.workbench import WORKBENCH_DEFAULT_BLOCKS, WorkbenchReadModel

router = APIRouter(tags=["execution"])

_VALID_BLOCKS = frozenset(WORKBENCH_DEFAULT_BLOCKS)


def _parse_include(include: Optional[str]) -> Optional[list[str]]:
    if not include:
        return None
    requested = [item.strip() for item in include.split(",") if item.strip()]
    unknown = [item for item in requested if item not in _VALID_BLOCKS]
    if unknown:
        raise ValueError(f"unknown workbench block(s): {', '.join(unknown)}")
    return requested or None


@router.get(
    "/execution/workbench",
    response_model=ApiResponse[WorkbenchPayload],
)
def execution_workbench(
    account_alias: str = Query(..., description="账户别名（必填，与本实例绑定）"),
    symbol: Optional[str] = Query(
        default=None, description="可选品种过滤（仅影响 marketContext）"
    ),
    include: Optional[str] = Query(
        default=None,
        description=(
            "CSV 块过滤，可选值："
            + ", ".join(WORKBENCH_DEFAULT_BLOCKS)
            + "；默认返回全部"
        ),
    ),
    workbench: WorkbenchReadModel = Depends(get_workbench_read_model),
) -> ApiResponse[WorkbenchPayload]:
    try:
        include_list = _parse_include(include)
    except ValueError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.INVALID_PARAMETER_VALUE,
            error_message=str(exc),
            suggested_action=AIErrorAction.VALIDATE_PARAMETERS,
            details={"valid_blocks": list(WORKBENCH_DEFAULT_BLOCKS)},
        )

    try:
        payload_dict = workbench.build(
            account_alias=account_alias,
            symbol=symbol,
            include=include_list,
        )
    except KeyError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.ACCOUNT_NOT_FOUND,
            error_message=str(exc),
            suggested_action=AIErrorAction.CHECK_ACCOUNT_STATUS,
            details={"account_alias": account_alias},
        )

    return ApiResponse.success_response(
        data=WorkbenchPayload(**payload_dict),
        metadata={
            "operation": "execution_workbench",
            "account_alias": account_alias,
            "symbol": symbol,
            "blocks": include_list or list(WORKBENCH_DEFAULT_BLOCKS),
            "tier_doc": "docs/design/quantx-data-freshness-tiering.md",
        },
    )
