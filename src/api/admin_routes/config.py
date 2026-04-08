from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from src.api import deps
from src.api.admin_schemas import ConfigView
from src.api.schemas import ApiResponse
from src.config import get_config_provenance_snapshot, get_effective_config_snapshot, get_risk_config
from src.config.signal import get_signal_config
from src.indicators.manager import UnifiedIndicatorManager

from .common import CONFIG_FILES, load_json_config
from .view_models import IndicatorsConfigView

router = APIRouter(prefix="/admin", tags=["admin"])


def _normalize_indicator_items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        raw_items = payload.get("indicators", [])
        if isinstance(raw_items, list):
            return [item for item in raw_items if isinstance(item, dict)]
    return []


@router.get("/config", response_model=ApiResponse[ConfigView])
def admin_config(section: Optional[str] = Query(default=None, description="按 section 过滤，例如 trading、signal、risk、api。")) -> ApiResponse[ConfigView]:
    effective = get_effective_config_snapshot()
    provenance = get_config_provenance_snapshot()
    if section:
        effective = {key: value for key, value in effective.items() if key == section}
        provenance = {key: value for key, value in provenance.items() if key == section}
    return ApiResponse.success_response(ConfigView(effective=effective, provenance=provenance, files=list(CONFIG_FILES)))


@router.get("/config/signal", response_model=ApiResponse[Dict[str, Any]])
def admin_config_signal() -> ApiResponse[Dict[str, Any]]:
    return ApiResponse.success_response(get_signal_config().model_dump())


@router.get("/config/risk", response_model=ApiResponse[Dict[str, Any]])
def admin_config_risk() -> ApiResponse[Dict[str, Any]]:
    return ApiResponse.success_response(get_risk_config().model_dump())


@router.get("/config/indicators", response_model=ApiResponse[IndicatorsConfigView])
def admin_config_indicators(
    display_only: bool = Query(default=False, description="仅返回 display 标记的指标。"),
    indicator_mgr: UnifiedIndicatorManager = Depends(deps.get_indicator_manager),
) -> ApiResponse[IndicatorsConfigView]:
    indicators = _normalize_indicator_items(load_json_config("indicators.json"))
    if display_only:
        indicators = [item for item in indicators if item.get("display", False)]
    intrabar_names: List[str] = []
    try:
        eligible = getattr(indicator_mgr, "_get_intrabar_eligible_names", None)
        if callable(eligible):
            intrabar_names = sorted(eligible())
    except Exception:
        pass
    return ApiResponse.success_response(
        IndicatorsConfigView(
            indicators=indicators,
            total_count=len(indicators),
            display_count=sum(1 for item in indicators if item.get("display", False)),
            intrabar_indicators=intrabar_names,
        )
    )
