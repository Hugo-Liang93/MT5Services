from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api import deps
from src.api.admin_schemas import DashboardOverview
from src.api.schemas import ApiResponse

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/dashboard", response_model=ApiResponse[DashboardOverview])
def admin_dashboard(runtime_views=Depends(deps.get_runtime_read_model)) -> ApiResponse[DashboardOverview]:
    overview = runtime_views.dashboard_overview(deps.get_startup_status())
    return ApiResponse.success_response(DashboardOverview(**overview))
