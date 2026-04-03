from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import get_runtime_mode_controller, get_runtime_read_model
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import ApiResponse, RuntimeModeRequest
from src.app_runtime.mode_controller import RuntimeModeController
from src.readmodels.runtime import RuntimeReadModel

from .view_models import RuntimeModeSummaryView, RuntimeModeUpdateView

router = APIRouter(tags=["trade"])


@router.get("/trade/runtime-mode", response_model=ApiResponse[RuntimeModeSummaryView])
def trade_runtime_mode_status(
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[RuntimeModeSummaryView]:
    return ApiResponse.success_response(
        data=runtime_views.runtime_mode_summary(),
        metadata={"operation": "trade_runtime_mode_status"},
    )


@router.post("/trade/runtime-mode", response_model=ApiResponse[RuntimeModeUpdateView])
def trade_runtime_mode_update(
    request: RuntimeModeRequest,
    controller: RuntimeModeController = Depends(get_runtime_mode_controller),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[RuntimeModeUpdateView]:
    try:
        snapshot = controller.apply_mode(request.mode, reason=request.reason or "api")
    except Exception as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.INVALID_PARAMETER,
            error_message=str(exc),
            suggested_action=AIErrorAction.CHECK_REQUEST_PARAMETERS,
            details={
                "operation": "trade_runtime_mode_update",
                "mode": request.mode,
            },
        )
    runtime_mode = dict(snapshot or {})
    runtime_mode.setdefault("status", "healthy")
    payload = {
        "runtime_mode": runtime_mode,
        "trading_state": runtime_views.trading_state_summary(
            pending_limit=20,
            position_limit=20,
        ),
    }
    return ApiResponse.success_response(
        data=payload,
        metadata={"operation": "trade_runtime_mode_update"},
    )
