"""P10.1: /v1/cockpit/overview canonical 总控台读端点。

替代 QuantX 前端当前对 `overview capital + directive + context + market + signals`
的本地聚合。首页一请求内即可回答：
- 能否继续交易（decision）
- 先处理谁（triage_queue）
- 风险集中处（exposure_map，含跨账户 contributors[]）
- 当前机会（opportunity_queue）
- 数据是否新鲜（data_health / freshness_hints）
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from src.api.deps import get_cockpit_read_model
from src.api.schemas import ApiResponse
from src.readmodels.cockpit import CockpitReadModel

router = APIRouter(tags=["cockpit"])


@router.get(
    "/cockpit/overview",
    response_model=ApiResponse[dict],
    summary="Cockpit 总控台 canonical 读模型（P10.1）",
)
def cockpit_overview(
    include: Optional[str] = Query(
        default=None,
        description=(
            "CSV 选择要返回的块：decision,triage_queue,market_guard,data_health,"
            "exposure_map,opportunity_queue,safe_actions。留空返回全部。"
        ),
    ),
    read_model: CockpitReadModel = Depends(get_cockpit_read_model),
) -> ApiResponse[dict]:
    requested_blocks: Optional[list[str]] = None
    if include:
        requested_blocks = [
            token.strip()
            for token in include.split(",")
            if token.strip()
        ] or None
    payload = read_model.build_overview(include=requested_blocks)
    return ApiResponse.success_response(
        data=payload,
        metadata={
            "operation": "cockpit_overview",
            "blocks": list(payload.keys()),
            "account_count": len(payload.get("accounts") or []),
            "source_kind": payload.get("source", {}).get("kind"),
        },
    )
