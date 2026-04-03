"""Admin API routes."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from src.api import deps
from src.api.admin_schemas import (
    ConfigView,
    DashboardOverview,
    StrategyDetail,
    StrategyPerformanceReport,
)
from src.api.schemas import ApiResponse
from src.config import (
    get_config_provenance_snapshot,
    get_effective_config_snapshot,
    get_risk_config,
    resolve_config_path,
)
from src.config.signal import get_signal_config
from src.indicators.manager import UnifiedIndicatorManager
from src.monitoring.pipeline import PipelineEvent, PipelineEventBus
from src.signals.evaluation.calibrator import ConfidenceCalibrator
from src.signals.evaluation.performance import StrategyPerformanceTracker
from src.signals.models import SignalEvent
from src.signals.orchestration import SignalRuntime
from src.signals.service import SignalModule

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

_CONFIG_FILES = [
    "app.ini",
    "signal.ini",
    "risk.ini",
    "market.ini",
    "db.ini",
    "mt5.ini",
    "ingest.ini",
    "storage.ini",
    "economic.ini",
    "cache.ini",
    "indicators.json",
    "composites.json",
]


def _load_json_config(filename: str) -> Any:
    path = resolve_config_path(filename)
    if not path:
        return []
    with open(path, encoding="utf-8") as file:
        return json.load(file)


def _build_strategy_detail(name: str, signal_svc: SignalModule) -> StrategyDetail:
    return StrategyDetail(**signal_svc.describe_strategy(name))


@router.get("/dashboard", response_model=ApiResponse[DashboardOverview])
def admin_dashboard(
    runtime_views=Depends(deps.get_runtime_read_model),
) -> ApiResponse[DashboardOverview]:
    overview = runtime_views.dashboard_overview(deps.get_startup_status())
    return ApiResponse.success_response(DashboardOverview(**overview))


@router.get("/config", response_model=ApiResponse[ConfigView])
def admin_config(
    section: Optional[str] = Query(
        default=None,
        description="按 section 过滤，例如 trading、signal、risk、api。",
    ),
) -> ApiResponse[ConfigView]:
    effective = get_effective_config_snapshot()
    provenance = get_config_provenance_snapshot()

    if section:
        effective = {key: value for key, value in effective.items() if key == section}
        provenance = {
            key: value for key, value in provenance.items() if key == section
        }

    return ApiResponse.success_response(
        ConfigView(
            effective=effective,
            provenance=provenance,
            files=list(_CONFIG_FILES),
        )
    )


@router.get("/config/signal", response_model=ApiResponse[Dict[str, Any]])
def admin_config_signal() -> ApiResponse[Dict[str, Any]]:
    return ApiResponse.success_response(get_signal_config().model_dump())


@router.get("/config/risk", response_model=ApiResponse[Dict[str, Any]])
def admin_config_risk() -> ApiResponse[Dict[str, Any]]:
    return ApiResponse.success_response(get_risk_config().model_dump())


@router.get("/config/indicators", response_model=ApiResponse[Dict[str, Any]])
def admin_config_indicators(
    enabled_only: bool = Query(default=False, description="仅返回已启用指标。"),
    indicator_mgr: UnifiedIndicatorManager = Depends(deps.get_indicator_manager),
) -> ApiResponse[Dict[str, Any]]:
    indicators: List[Dict[str, Any]] = _load_json_config("indicators.json")
    if enabled_only:
        indicators = [item for item in indicators if item.get("enabled", True)]

    intrabar_names: List[str] = []
    try:
        eligible = getattr(indicator_mgr, "_get_intrabar_eligible_names", None)
        if callable(eligible):
            intrabar_names = sorted(eligible())
    except Exception:
        pass

    return ApiResponse.success_response(
        {
            "indicators": indicators,
            "total_count": len(indicators),
            "enabled_count": sum(
                1 for item in indicators if item.get("enabled", True)
            ),
            "intrabar_indicators": intrabar_names,
        }
    )


@router.get("/config/composites", response_model=ApiResponse[List[Dict[str, Any]]])
def admin_config_composites() -> ApiResponse[List[Dict[str, Any]]]:
    return ApiResponse.success_response(_load_json_config("composites.json"))


@router.get(
    "/performance/strategies",
    response_model=ApiResponse[StrategyPerformanceReport],
)
def admin_performance_strategies(
    hours: int = Query(default=168, ge=1, le=720, description="历史统计范围，单位小时。"),
    perf_tracker: StrategyPerformanceTracker = Depends(deps.get_performance_tracker),
    calibrator: ConfidenceCalibrator = Depends(deps.get_calibrator),
    signal_svc: SignalModule = Depends(deps.get_signal_service),
) -> ApiResponse[StrategyPerformanceReport]:
    try:
        ranking = perf_tracker.strategy_ranking()
    except Exception:
        ranking = []

    try:
        summary = perf_tracker.describe()
    except Exception:
        summary = {}

    try:
        winrates = signal_svc.strategy_winrates(hours=hours)
    except Exception:
        winrates = []

    try:
        calibrator_info = calibrator.describe()
    except Exception:
        calibrator_info = {}

    report = StrategyPerformanceReport(
        session_ranking=ranking,
        session_summary=summary,
        historical_winrates=winrates,
        calibrator=calibrator_info,
    )
    return ApiResponse.success_response(report)


@router.get(
    "/performance/confidence-pipeline/{symbol}/{timeframe}",
    response_model=ApiResponse[Dict[str, Any]],
)
def admin_confidence_pipeline(
    symbol: str,
    timeframe: str,
    signal_runtime: SignalRuntime = Depends(deps.get_signal_runtime),
    signal_svc: SignalModule = Depends(deps.get_signal_service),
    perf_tracker: StrategyPerformanceTracker = Depends(deps.get_performance_tracker),
    calibrator: ConfidenceCalibrator = Depends(deps.get_calibrator),
) -> ApiResponse[Dict[str, Any]]:
    regime_info = signal_runtime.get_regime_stability(symbol, timeframe) or {}
    strategies_pipeline: List[Dict[str, Any]] = []

    for descriptor in signal_svc.strategy_catalog():
        name = str(descriptor["name"])
        try:
            stats = perf_tracker.get_strategy_stats(name)
        except Exception:
            stats = None

        multiplier = 1.0
        try:
            multiplier = perf_tracker.get_multiplier(name)
        except Exception:
            pass

        strategies_pipeline.append(
            {
                **descriptor,
                "session_multiplier": multiplier,
                "session_stats": stats,
            }
        )

    return ApiResponse.success_response(
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "regime": regime_info,
            "calibrator": calibrator.describe(),
            "strategies": strategies_pipeline,
        }
    )


@router.get("/strategies", response_model=ApiResponse[List[StrategyDetail]])
def admin_strategies(
    signal_svc: SignalModule = Depends(deps.get_signal_service),
) -> ApiResponse[List[StrategyDetail]]:
    result = [StrategyDetail(**row) for row in signal_svc.strategy_catalog()]
    return ApiResponse.success_response(result)


@router.get("/strategies/{name}", response_model=ApiResponse[Dict[str, Any]])
def admin_strategy_detail(
    name: str,
    signal_svc: SignalModule = Depends(deps.get_signal_service),
    perf_tracker: StrategyPerformanceTracker = Depends(deps.get_performance_tracker),
) -> ApiResponse[Dict[str, Any]]:
    if name not in signal_svc.list_strategies():
        return ApiResponse.error_response(
            error_code="VALIDATION_ERROR",
            error_message=f"Strategy '{name}' not found",
            suggested_action="Check /v1/admin/strategies for available strategies",
        )

    detail = _build_strategy_detail(name, signal_svc)

    try:
        stats = perf_tracker.get_strategy_stats(name)
    except Exception:
        stats = None

    return ApiResponse.success_response(
        {
            **detail.model_dump(),
            "session_performance": stats,
        }
    )


@router.get("/events/stream")
async def admin_events_stream(
    scope: str = Query(
        default="all",
        pattern="^(confirmed|intrabar|all)$",
        description="按 confirmed、intrabar、all 过滤。",
    ),
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

    async def event_generator():  # type: ignore[no-untyped-def]
        heartbeat_interval = 30.0
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(
                        queue.get(),
                        timeout=heartbeat_interval,
                    )
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    heartbeat = {
                        "type": "heartbeat",
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }
                    yield f"data: {json.dumps(heartbeat, ensure_ascii=False)}\n\n"
        finally:
            signal_runtime.remove_signal_listener(_on_signal)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/pipeline/stream")
async def admin_pipeline_stream(
    scope: str = Query(
        default="all",
        pattern="^(confirmed|intrabar|all)$",
        description="按 confirmed、intrabar、all 过滤。",
    ),
    symbol: Optional[str] = Query(default=None, description="按品种过滤。"),
    detail: bool = Query(
        default=False,
        description="为 true 时返回完整 OHLC 与指标快照。",
    ),
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

        payload: Dict[str, Any] = {
            "type": event.type,
            "trace_id": event.trace_id,
            "symbol": event.symbol,
            "timeframe": event.timeframe,
            "scope": event.scope,
            "ts": event.ts,
            **event.payload,
        }
        if not detail:
            for key in detail_keys:
                payload.pop(key, None)

        try:
            loop.call_soon_threadsafe(queue.put_nowait, payload)
        except (RuntimeError, asyncio.QueueFull):
            pass

    pipeline_bus.add_listener(_on_pipeline_event)

    async def event_generator():  # type: ignore[no-untyped-def]
        heartbeat_interval = 15.0
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(
                        queue.get(),
                        timeout=heartbeat_interval,
                    )
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    heartbeat = {
                        "type": "heartbeat",
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }
                    yield f"data: {json.dumps(heartbeat, ensure_ascii=False)}\n\n"
        finally:
            pipeline_bus.remove_listener(_on_pipeline_event)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/pipeline/stats")
async def admin_pipeline_stats(
    pipeline_bus: PipelineEventBus = Depends(deps.get_pipeline_event_bus),
) -> ApiResponse[Dict[str, Any]]:
    return ApiResponse.success_response(pipeline_bus.stats())
