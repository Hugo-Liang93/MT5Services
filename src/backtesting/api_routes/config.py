from __future__ import annotations

from typing import Optional

from fastapi import APIRouter

from src.api.schemas import ApiResponse
from src.backtesting import api_config

router = APIRouter()


@router.get("/config/defaults", response_model=ApiResponse)
async def get_backtest_config_defaults() -> ApiResponse:
    defaults = api_config.load_backtest_defaults()
    return ApiResponse(
        success=True,
        data={
            "defaults": defaults,
            "supported": {
                "search_modes": list(api_config.SEARCH_MODES),
                "sort_metrics": list(api_config.SORT_METRICS),
                "run_fields": [
                    "symbol",
                    "timeframe",
                    "start_time",
                    "end_time",
                    "strategies",
                    *api_config.CONFIG_OVERRIDE_FIELDS,
                    "strategy_params",
                    "strategy_params_per_tf",
                    "regime_affinity_overrides",
                ],
                "optimize_fields": [
                    "param_space",
                    "search_mode",
                    "max_combinations",
                    "sort_metric",
                ],
                "walk_forward_fields": [
                    "n_splits",
                    "train_ratio",
                    "anchored",
                ],
            },
        },
    )


@router.get("/config/param-space-template", response_model=ApiResponse)
async def get_backtest_param_space_template(
    timeframe: str,
    strategies: Optional[str] = None,
) -> ApiResponse:
    requested = None
    if strategies:
        requested = [item.strip() for item in strategies.split(",") if item.strip()]
    return ApiResponse(
        success=True,
        data=api_config.build_param_space_template(timeframe, requested),
    )
