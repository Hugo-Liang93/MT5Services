from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse

from src.api.schemas import ApiResponse
from src.studio.service import StudioService
from src.utils.timezone import utc_now

from .common import get_studio_service

router = APIRouter(prefix="/studio", tags=["studio"])

_AGENT_REFRESH_SECONDS = 3.0
_HEARTBEAT_SECONDS = 30.0
_DIFF_KEYS = ("status", "task", "alertLevel", "metrics")
_MAX_SSE_CONNECTIONS = 5


def _agent_changed(prev: dict[str, Any], curr: dict[str, Any]) -> bool:
    return any(prev.get(key) != curr.get(key) for key in _DIFF_KEYS)


def _format_sse(event: str, data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


@router.get("/stream")
async def studio_stream(
    studio: StudioService = Depends(get_studio_service),
) -> StreamingResponse:
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
            yield _format_sse("snapshot", studio.build_snapshot(force_refresh=True))

            last_agents: dict[str, dict[str, Any]] = {
                str(agent["id"]): agent for agent in studio.build_agents()
            }
            last_refresh = time.monotonic()
            last_heartbeat = time.monotonic()

            while True:
                now = time.monotonic()
                next_refresh = last_refresh + _AGENT_REFRESH_SECONDS - now
                next_heartbeat = last_heartbeat + _HEARTBEAT_SECONDS - now
                timeout = max(0.1, min(next_refresh, next_heartbeat))

                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=timeout)
                    yield _format_sse(msg["type"], msg["payload"])
                except asyncio.TimeoutError:
                    pass

                now = time.monotonic()

                if now - last_refresh >= _AGENT_REFRESH_SECONDS:
                    last_refresh = now
                    snapshot = studio.refresh_snapshot()
                    for agent in snapshot["agents"]:
                        agent_id = str(agent.get("id", ""))
                        prev = last_agents.get(agent_id)
                        if prev is None or _agent_changed(prev, agent):
                            yield _format_sse("agent_update", agent)
                            last_agents[agent_id] = agent

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
