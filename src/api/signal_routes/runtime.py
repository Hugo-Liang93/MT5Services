from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict

from fastapi import APIRouter, Depends, Query

from src.api.deps import (
    get_calibrator,
    get_htf_cache,
    get_market_service,
    get_market_structure_analyzer,
    get_runtime_read_model,
    get_signal_runtime,
    get_signal_service,
)
from src.api.schemas import ApiResponse
from src.market import MarketDataService
from src.market_structure import MarketStructureAnalyzer
from src.readmodels.runtime import RuntimeReadModel
from src.signals.evaluation.calibrator import ConfidenceCalibrator
from src.signals.orchestration import SignalRuntime
from src.signals.service import SignalModule
from src.signals.strategies.htf_cache import HTFStateCache
from .view_models import (
    CalibratorStatusView,
    HTFCacheStatusView,
    MarketStructureView,
    RegimeReportView,
    SignalRuntimeSummaryView,
    TrackedPositionsView,
    VotingStatsView,
)

router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("/runtime/status", response_model=ApiResponse[SignalRuntimeSummaryView])
def signal_runtime_status(
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[dict]:
    return ApiResponse.success_response(data=runtime_views.signal_runtime_summary())


@router.get("/positions", response_model=ApiResponse[TrackedPositionsView])
def get_tracked_positions(
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[dict]:
    payload = runtime_views.tracked_positions_payload(limit=100)
    return ApiResponse.success_response(
        data=payload,
        metadata={
            "count": payload["count"],
            "position_manager_status": payload["manager"]["status"],
        },
    )


@router.get("/regime/{symbol}/{timeframe}", response_model=ApiResponse[RegimeReportView])
def get_regime(
    symbol: str,
    timeframe: str,
    service: SignalModule = Depends(get_signal_service),
    runtime: SignalRuntime = Depends(get_signal_runtime),
) -> ApiResponse[Dict[str, object]]:
    return ApiResponse.success_response(
        data=service.regime_report(symbol=symbol, timeframe=timeframe, runtime=runtime)
    )


@router.get("/market-structure/{symbol}/{timeframe}", response_model=ApiResponse[MarketStructureView])
def get_market_structure(
    symbol: str,
    timeframe: str,
    analyzer: MarketStructureAnalyzer = Depends(get_market_structure_analyzer),
    market_service: MarketDataService = Depends(get_market_service),
) -> ApiResponse[Dict[str, object]]:
    event_time = datetime.now(timezone.utc)
    latest_close: float | None = None
    price_source = "latest_closed_bar"
    try:
        quote = market_service.get_quote(symbol)
    except Exception:
        quote = None
    if quote is not None:
        raw_last = getattr(quote, "last", None)
        try:
            latest_close = float(raw_last) if raw_last is not None else None
        except (TypeError, ValueError):
            latest_close = None
        if latest_close is not None and latest_close > 0:
            price_source = "live_quote_last"
        else:
            try:
                bid = float(getattr(quote, "bid", 0.0) or 0.0)
                ask = float(getattr(quote, "ask", 0.0) or 0.0)
            except (TypeError, ValueError):
                bid = 0.0
                ask = 0.0
            if bid > 0 and ask > 0:
                latest_close = (bid + ask) / 2.0
                price_source = "live_quote_mid"
    return ApiResponse.success_response(
        data=analyzer.analyze(
            symbol,
            timeframe,
            event_time=event_time,
            latest_close=latest_close,
        ),
        metadata={
            "symbol": symbol,
            "timeframe": timeframe,
            "analysis_mode": "live_quote" if latest_close is not None else "closed_bar_fallback",
            "price_source": price_source,
            "event_time": event_time.isoformat(),
        },
    )


@router.get("/voting/stats", response_model=ApiResponse[VotingStatsView])
def voting_stats(
    runtime: SignalRuntime = Depends(get_signal_runtime),
) -> ApiResponse[Dict[str, object]]:
    voting_info = runtime.get_voting_info()
    regime_stability = runtime.get_regime_stability_map()
    return ApiResponse.success_response(
        data={
            "voting_enabled": voting_info.get("voting_enabled", False),
            "voting_config": voting_info.get("voting_config"),
            "regime_stability": regime_stability,
        }
    )


@router.get("/htf/cache", response_model=ApiResponse[HTFCacheStatusView])
def htf_cache_status(
    htf_cache: HTFStateCache = Depends(get_htf_cache),
) -> ApiResponse[Dict[str, object]]:
    return ApiResponse.success_response(data=htf_cache.describe())


@router.get("/calibrator/status", response_model=ApiResponse[CalibratorStatusView])
def calibrator_status(
    calibrator: ConfidenceCalibrator = Depends(get_calibrator),
) -> ApiResponse[Dict[str, object]]:
    return ApiResponse.success_response(data=calibrator.describe())


@router.post("/calibrator/refresh", response_model=ApiResponse[CalibratorStatusView])
def calibrator_refresh(
    hours: int = Query(default=168, ge=24, le=24 * 90),
    calibrator: ConfidenceCalibrator = Depends(get_calibrator),
) -> ApiResponse[Dict[str, object]]:
    calibrator._refresh_hours = hours
    count = calibrator.refresh()
    return ApiResponse.success_response(
        data={**calibrator.describe(), "rows_loaded": count},
        metadata={"hours": hours},
    )
