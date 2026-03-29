from __future__ import annotations

from fastapi import APIRouter

from src.api.schemas import ApiResponse, DecisionBriefModel, DecisionBriefRequest
from src.decision import build_decision_brief

router = APIRouter(prefix="/decision", tags=["decision"])


@router.post("/brief", response_model=ApiResponse[DecisionBriefModel])
def decision_brief(request: DecisionBriefRequest) -> ApiResponse[DecisionBriefModel]:
    brief = DecisionBriefModel(**build_decision_brief(request.context.model_dump()))
    return ApiResponse.success_response(
        data=brief,
        metadata={"provider": "backend_heuristic_engine"},
    )
