from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict

from fastapi import APIRouter, Depends, Query

from src.api.deps import (
    get_calibrator,
    get_htf_cache,
    get_market_service,
    get_market_structure_analyzer,
    get_performance_tracker,
    get_runtime_read_model,
    get_health_monitor_instance,
    get_signal_runtime,
    get_signal_service,
)
from src.api.schemas import ApiResponse
from src.market import MarketDataService
from src.market_structure import MarketStructureAnalyzer
from src.readmodels.runtime import RuntimeReadModel
from src.signals.evaluation.calibrator import ConfidenceCalibrator
from src.signals.orchestration.runtime import SignalRuntime
from src.signals.service import SignalModule
from src.signals.evaluation.performance import StrategyPerformanceTracker
from src.signals.strategies.htf_cache import HTFStateCache
from src.monitoring.health.monitor import HealthMonitor
from .view_models import (
    CalibratorStatusView,
    IntrabarSLOPoint,
    IntrabarSLOWindowView,
    HTFCacheStatusView,
    MarketStructureView,
    RegimeReportView,
    SignalRuntimeSummaryView,
    TrackedPositionsView,
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
    # Quote 契约（src/clients/mt5_market.py:Quote）：bid/ask/last 为 float 必填字段。
    # 只有 market_service 尚未拿到 quote（首次请求 / MT5 断线）才会返回 None。
    quote = market_service.get_quote(symbol)
    if quote is not None:
        if quote.last > 0:
            latest_close = quote.last
            price_source = "live_quote_last"
        elif quote.bid > 0 and quote.ask > 0:
            latest_close = (quote.bid + quote.ask) / 2.0
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
    # §0y P2：旧实现 calibrator._refresh_hours = hours 后调 refresh() 把后续
    # 后台刷新和默认 refresh 主窗口都永久改掉。改用一次性 hours= 参数，
    # 不污染服务内部状态（参 ADR-006：装配/API 层禁止读写组件 _ 前缀私有属性）。
    count = calibrator.refresh(hours=hours)
    return ApiResponse.success_response(
        data={**calibrator.describe(), "rows_loaded": count},
        metadata={"hours": hours},
    )


@router.get(
    "/performance-tracker",
    response_model=ApiResponse[dict],
    summary="日内策略绩效追踪器状态",
)
async def get_performance_tracker_status(
    perf: StrategyPerformanceTracker = Depends(get_performance_tracker),
) -> ApiResponse[dict]:
    return ApiResponse.success_response(data=perf.describe())


@router.get(
    "/runtime/intrabar-slos",
    response_model=ApiResponse[IntrabarSLOWindowView],
    summary="获取 intrabar SLO 指标时间窗口",
)
def get_intrabar_slos_timeseries(
    component: str = Query(default="indicator_calculation"),
    limit: int = Query(default=120, ge=1, le=5000),
    health_monitor: HealthMonitor = Depends(get_health_monitor_instance),
) -> ApiResponse[IntrabarSLOWindowView]:
    def _normalize_samples(metric_name: str) -> list[IntrabarSLOPoint]:
        points: list[IntrabarSLOPoint] = []
        for sample in health_monitor.get_recent_metrics(
            component=component,
            metric_name=metric_name,
            limit=limit,
        ):
            if not isinstance(sample, dict):
                continue
            if sample.get("timestamp") is None or sample.get("value") is None:
                continue
            try:
                alert_level = sample.get("alert_level")
                points.append(
                    IntrabarSLOPoint(
                        timestamp=str(sample["timestamp"]),
                        value=float(sample["value"]),
                        alert_level=alert_level if isinstance(alert_level, str) else None,
                    )
                )
            except (TypeError, ValueError):
                continue
        return points

    return ApiResponse.success_response(
        data=IntrabarSLOWindowView(
            component=component,
            limit=limit,
            drop_rate=_normalize_samples("intrabar_drop_rate_1m"),
            queue_age_ms_p95=_normalize_samples("intrabar_queue_age_p95_ms"),
            to_decision_latency_ms_p95=_normalize_samples(
                "intrabar_to_decision_latency_p95_ms"
            ),
        ).model_dump(),
        metadata={"component": component, "metric_count": 3},
    )
