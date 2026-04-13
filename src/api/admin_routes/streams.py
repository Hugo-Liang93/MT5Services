from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from src.api import deps
from src.api.schemas import ApiResponse
from src.monitoring.pipeline import PipelineEvent, PipelineEventBus
from src.signals.models import SignalEvent
from src.signals.orchestration.runtime import SignalRuntime
from .view_models import PipelineStatsView

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/events/stream")
async def admin_events_stream(
    scope: str = Query(default="all", pattern="^(confirmed|intrabar|all)$", description="按 confirmed、intrabar、all 过滤。"),
    symbol: Optional[str] = Query(default=None, description="按品种过滤。"),
    signal_runtime: SignalRuntime = Depends(deps.get_signal_runtime),
) -> StreamingResponse:
    queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=256)
    loop = asyncio.get_running_loop()

    def _on_signal(event: SignalEvent) -> None:
        if scope != "all" and event.scope != scope:
            return
        if symbol and event.symbol != symbol:
            return
        payload = {
            "type": "signal",
            "trace_id": (event.metadata or {}).get("signal_trace_id", ""),
            "signal_id": event.signal_id,
            "symbol": event.symbol,
            "timeframe": event.timeframe,
            "strategy": event.strategy,
            "direction": event.direction,
            "confidence": round(event.confidence, 4),
            "signal_state": event.signal_state,
            "scope": event.scope,
            "reason": event.reason,
            "generated_at": event.generated_at.isoformat(),
        }
        try:
            loop.call_soon_threadsafe(queue.put_nowait, payload)
        except (RuntimeError, asyncio.QueueFull):
            pass

    signal_runtime.add_signal_listener(_on_signal)

    async def event_generator():
        heartbeat_interval = 30.0
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval)
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    heartbeat = {"type": "heartbeat", "ts": datetime.now(timezone.utc).isoformat()}
                    yield f"data: {json.dumps(heartbeat, ensure_ascii=False)}\n\n"
        finally:
            signal_runtime.remove_signal_listener(_on_signal)

    return StreamingResponse(event_generator(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})


@router.get("/pipeline/stream")
async def admin_pipeline_stream(
    scope: str = Query(default="all", pattern="^(confirmed|intrabar|all)$", description="按 confirmed、intrabar、all 过滤。"),
    symbol: Optional[str] = Query(default=None, description="按品种过滤。"),
    detail: bool = Query(default=False, description="为 true 时返回完整 OHLC 与指标快照。"),
    pipeline_bus: PipelineEventBus = Depends(deps.get_pipeline_event_bus),
) -> StreamingResponse:
    detail_keys = {"ohlc", "indicator_names", "indicators"}
    queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=512)
    loop = asyncio.get_running_loop()

    def _on_pipeline_event(event: PipelineEvent) -> None:
        if scope != "all" and event.scope != scope:
            return
        if symbol and event.symbol != symbol:
            return
        payload: Dict[str, Any] = {"type": event.type, "trace_id": event.trace_id, "symbol": event.symbol, "timeframe": event.timeframe, "scope": event.scope, "ts": event.ts, **event.payload}
        if not detail:
            for key in detail_keys:
                payload.pop(key, None)
        try:
            loop.call_soon_threadsafe(queue.put_nowait, payload)
        except (RuntimeError, asyncio.QueueFull):
            pass

    pipeline_bus.add_listener(_on_pipeline_event)

    async def event_generator():
        heartbeat_interval = 15.0
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval)
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    heartbeat = {"type": "heartbeat", "ts": datetime.now(timezone.utc).isoformat()}
                    yield f"data: {json.dumps(heartbeat, ensure_ascii=False)}\n\n"
        finally:
            pipeline_bus.remove_listener(_on_pipeline_event)

    return StreamingResponse(event_generator(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})


@router.get("/pipeline/stats", response_model=ApiResponse[PipelineStatsView])
async def admin_pipeline_stats(
    pipeline_bus: PipelineEventBus = Depends(deps.get_pipeline_event_bus),
) -> ApiResponse[PipelineStatsView]:
    return ApiResponse.success_response(PipelineStatsView(**pipeline_bus.stats()))
