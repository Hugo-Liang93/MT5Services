from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from src.api.error_codes import AIErrorCode
from src.api.schemas import ApiResponse
from src.studio.service import StudioService

from .common import get_studio_service

router = APIRouter(prefix="/studio", tags=["studio"])


@router.get("/agents")
def studio_agents(
    studio: StudioService = Depends(get_studio_service),
) -> ApiResponse[list[dict[str, Any]]]:
    agents = studio.build_agents()
    return ApiResponse.success_response(data=agents, metadata={"count": len(agents)})


@router.get("/agents/{agent_id}")
def studio_agent_detail(
    agent_id: str,
    studio: StudioService = Depends(get_studio_service),
) -> ApiResponse[Optional[dict[str, Any]]]:
    agents = studio.build_agents()
    for agent in agents:
        if agent.get("id") == agent_id:
            return ApiResponse.success_response(data=agent)
    return ApiResponse.error_response(
        error_code=AIErrorCode.NOT_FOUND,
        error_message=f"Agent '{agent_id}' not found",
    )


@router.get("/events")
def studio_events(
    limit: int = Query(default=50, ge=1, le=500),
    studio: StudioService = Depends(get_studio_service),
) -> ApiResponse[list[dict[str, Any]]]:
    events = studio.recent_events(limit)
    return ApiResponse.success_response(data=events, metadata={"count": len(events)})


@router.get("/summary")
def studio_summary(
    studio: StudioService = Depends(get_studio_service),
) -> ApiResponse[dict[str, Any]]:
    return ApiResponse.success_response(data=studio.build_summary())
