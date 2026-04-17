"""Admin routes for the notification module.

- ``GET  /admin/notifications/status`` — runtime state + outbox + metrics.
- ``POST /admin/notifications/toggle`` — runtime on/off without process restart.

Kept intentionally small; deeper inbox/DLQ inspection lives in a future
``/admin/notifications/outbox`` once we have UI needs.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api import deps
from src.api.schemas import ApiResponse
from src.notifications.module import NotificationModule

router = APIRouter(prefix="/admin/notifications", tags=["admin", "notifications"])


class NotificationToggleRequest(BaseModel):
    enabled: bool = Field(
        ...,
        description="true 开启（启动 worker + 注册 listener）；false 关闭（停 worker + 解除监听）。",
    )


@router.get("/status", response_model=ApiResponse[Dict[str, Any]])
def notifications_status(
    module: NotificationModule | None = Depends(deps.get_notification_module),
) -> ApiResponse[Dict[str, Any]]:
    if module is None:
        return ApiResponse.success_response(
            {
                "available": False,
                "reason": "notification module not configured (bot_token/chat_id missing)",
            }
        )
    snapshot: Dict[str, Any] = {"available": True, **module.status()}
    return ApiResponse.success_response(snapshot)


@router.post("/toggle", response_model=ApiResponse[Dict[str, Any]])
def notifications_toggle(
    request: NotificationToggleRequest,
    module: NotificationModule | None = Depends(deps.get_notification_module),
) -> ApiResponse[Dict[str, Any]]:
    if module is None:
        raise HTTPException(
            status_code=503,
            detail="notification module not configured",
        )
    try:
        module.set_enabled(request.enabled)
    except Exception as exc:  # noqa: BLE001 — surface the reason to operator
        raise HTTPException(status_code=500, detail=f"toggle failed: {exc}") from exc
    return ApiResponse.success_response(
        {"requested_enabled": request.enabled, **module.status()}
    )
