"""P10.5: /v1/lab/impact Lab 工作台贯通读端点。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from src.api.deps import get_lab_impact_read_model
from src.api.schemas import ApiResponse
from src.readmodels.lab_impact import LabImpactReadModel

router = APIRouter(tags=["lab"])


@router.get(
    "/lab/impact",
    response_model=ApiResponse[dict],
    summary="Lab impact 贯通读模型（P10.5）",
    description=(
        "单端点返回 walk-forward 快照（含分阶段指标） / recommendations（含 "
        "lifecycle + linked paper sessions） / paper sessions（含 "
        "recommendation_id / source_backtest_run_id / experiment_id） / "
        "experiment_links（按 experiment_id 聚合研究链路）。替代 QuantX `Lab` "
        "工作台对多路 API 的本地拼接。"
    ),
)
def lab_impact(
    wf_limit: int = Query(default=10, ge=1, le=50),
    recommendation_limit: int = Query(default=20, ge=1, le=100),
    paper_session_limit: int = Query(default=20, ge=1, le=100),
    read_model: LabImpactReadModel = Depends(get_lab_impact_read_model),
) -> ApiResponse[dict]:
    payload = read_model.build_impact(
        wf_limit=wf_limit,
        recommendation_limit=recommendation_limit,
        paper_session_limit=paper_session_limit,
    )
    return ApiResponse.success_response(
        data=payload,
        metadata={
            "operation": "lab_impact",
            "walk_forward_count": len(payload["walk_forward_snapshots"]),
            "recommendation_count": len(payload["recommendations"]),
            "paper_session_count": len(payload["paper_sessions"]),
            "experiment_link_count": len(payload["experiment_links"]),
        },
    )
