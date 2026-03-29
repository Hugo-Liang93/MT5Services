"""Studio API — REST + SSE endpoints for the Anteater 3D frontend.

REST endpoints:
    GET /studio/agents          → current status of all 10 agents
    GET /studio/events          → recent events from ring buffer
    GET /studio/summary         → aggregated summary for TopBar
    GET /studio/agents/{id}     → single agent detail

SSE endpoint:
    GET /studio/stream          → real-time push (snapshot + diffs + events)
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, StreamingResponse

from src.api.error_codes import AIErrorCode
from src.api.schemas import ApiResponse
from src.studio.service import StudioService
from src.utils.timezone import utc_now

router = APIRouter(prefix="/studio", tags=["studio"])

# ── Dependency ─────────────────────────────────────────────────


def _get_studio_service() -> StudioService:
    from src.api.deps import get_studio_service
    return get_studio_service()


# ── REST endpoints ─────────────────────────────────────────────


@router.get("/agents")
def studio_agents(
    studio: StudioService = Depends(_get_studio_service),
) -> ApiResponse[list[dict[str, Any]]]:
    agents = studio.build_agents()
    return ApiResponse.success_response(
        data=agents,
        metadata={"count": len(agents)},
    )


@router.get("/agents/{agent_id}")
def studio_agent_detail(
    agent_id: str,
    studio: StudioService = Depends(_get_studio_service),
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
    studio: StudioService = Depends(_get_studio_service),
) -> ApiResponse[list[dict[str, Any]]]:
    events = studio.recent_events(limit)
    return ApiResponse.success_response(
        data=events,
        metadata={"count": len(events)},
    )


@router.get("/summary")
def studio_summary(
    studio: StudioService = Depends(_get_studio_service),
) -> ApiResponse[dict[str, Any]]:
    return ApiResponse.success_response(data=studio.build_summary())


# ── SSE endpoint ───────────────────────────────────────────────

# Agent diff-push interval (seconds)
_AGENT_REFRESH_SECONDS = 3.0
# Heartbeat interval to keep connection alive
_HEARTBEAT_SECONDS = 30.0
# Fields compared for change detection (excludes updatedAt which always changes)
_DIFF_KEYS = ("status", "task", "alertLevel", "metrics")


def _agent_changed(prev: dict[str, Any], curr: dict[str, Any]) -> bool:
    """Check if an agent's visible state has changed."""
    return any(prev.get(k) != curr.get(k) for k in _DIFF_KEYS)


def _format_sse(event: str, data: Any) -> str:
    """Format a single SSE frame with named event type."""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


_MAX_SSE_CONNECTIONS = 5


@router.get("/stream")
async def studio_stream(
    studio: StudioService = Depends(_get_studio_service),
) -> StreamingResponse:
    """SSE stream providing real-time Studio updates.

    Protocol (matches frontend ``wsHandlers.ts`` message types):
        - ``snapshot``: full initial state (agents + events + summary)
        - ``agent_update``: single agent changed
        - ``event_append``: new event from ring buffer
        - ``heartbeat``: keep-alive
    """
    if studio.subscriber_count >= _MAX_SSE_CONNECTIONS:
        return JSONResponse(
            status_code=429,
            content=ApiResponse.error_response(
                error_code="too_many_sse_connections",
                error_message=f"Too many SSE connections (max {_MAX_SSE_CONNECTIONS})",
            ).model_dump(),
            headers={"Retry-After": "5"},
        )

    loop = asyncio.get_running_loop()
    queue = studio.subscribe(loop)

    async def event_generator():  # type: ignore[no-untyped-def]
        try:
            # 1. Initial full snapshot
            yield _format_sse("snapshot", studio.build_snapshot(force_refresh=True))

            # Track last-sent agent states for diff detection
            last_agents: dict[str, dict[str, Any]] = {
                str(a["id"]): a for a in studio.build_agents()
            }
            last_refresh = time.monotonic()
            last_heartbeat = time.monotonic()

            while True:
                # Calculate next timeout
                now = time.monotonic()
                next_refresh = last_refresh + _AGENT_REFRESH_SECONDS - now
                next_heartbeat = last_heartbeat + _HEARTBEAT_SECONDS - now
                timeout = max(0.1, min(next_refresh, next_heartbeat))

                # Wait for pushed events or timeout
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=timeout)
                    yield _format_sse(msg["type"], msg["payload"])
                except asyncio.TimeoutError:
                    pass

                now = time.monotonic()

                # Periodic agent diff-push
                if now - last_refresh >= _AGENT_REFRESH_SECONDS:
                    last_refresh = now
                    snapshot = studio.refresh_snapshot()
                    for agent in snapshot["agents"]:
                        aid = str(agent.get("id", ""))
                        prev = last_agents.get(aid)
                        if prev is None or _agent_changed(prev, agent):
                            yield _format_sse("agent_update", agent)
                            last_agents[aid] = agent

                # Heartbeat
                if now - last_heartbeat >= _HEARTBEAT_SECONDS:
                    last_heartbeat = now
                    yield _format_sse("heartbeat", {"ts": utc_now().isoformat()})

        finally:
            studio.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
