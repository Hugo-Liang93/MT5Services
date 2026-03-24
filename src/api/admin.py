"""Admin 管理仪表板 API 路由。

提供仪表板概览、配置查看（只读）、绩效报表、策略详情和 SSE 实时事件推送。
所有端点仅挂载在 ``/v1/admin`` 下，不向后兼容挂载到根路径。
"""

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
    ExecutorSnapshot,
    StrategyDetail,
    StrategyPerformanceReport,
    SystemStatusSnapshot,
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
from src.signals.evaluation.calibrator import ConfidenceCalibrator
from src.signals.evaluation.performance import StrategyPerformanceTracker
from src.signals.models import SignalEvent
from src.signals.orchestration import SignalRuntime
from src.signals.service import SignalModule
from src.trading.position_manager import PositionManager
from src.trading.service import TradingModule
from src.trading.signal_executor import TradeExecutor

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# ── 配置文件列表 ──────────────────────────────────────────────

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
    """通过 resolve_config_path 安全读取 JSON 配置文件。"""
    path = resolve_config_path(filename)
    if not path:
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _build_strategy_detail(
    name: str,
    signal_svc: SignalModule,
) -> StrategyDetail:
    """使用 SignalModule public API 构建策略详情（不访问私有属性）。"""
    affinity_map = signal_svc.strategy_affinity_map(name)
    affinity_dict = (
        {k.value: v for k, v in affinity_map.items()} if affinity_map else {}
    )

    try:
        scopes = list(signal_svc.strategy_scopes(name))
    except ValueError:
        scopes = []

    try:
        indicators = list(signal_svc.strategy_requirements(name))
    except ValueError:
        indicators = []

    category = ""
    if name in signal_svc._strategy_affinity_cache:
        # category 没有独立 public API，但 affinity_cache 是 SignalModule 内部缓存。
        # 退而使用 describe() 中可能暴露的信息——但实际 category 只能通过 getattr 读取。
        pass
    # category 通过 strategy_affinity_cache 的 key 间接可达，但更直接的方式是
    # 通过 list_strategies() 返回的 name 查询。由于 SignalModule 没有
    # get_strategy_category() public API，我们需要间接获取。
    # 最安全的方式：调用 describe() 中已有的信息，或者用 strategy_ranking() 里的 category。
    # 这里折中：使用 perf_tracker.describe() 中 categories 信息（如果可用），
    # 或者直接不提供 category（前端可从 /performance/strategies 获取）。

    return StrategyDetail(
        name=name,
        category=category,
        preferred_scopes=scopes,
        required_indicators=indicators,
        regime_affinity=affinity_dict,
    )


# ═══════════════════════════════════════════════════════════════
# 3.1  仪表板概览
# ═══════════════════════════════════════════════════════════════


@router.get("/dashboard", response_model=ApiResponse[DashboardOverview])
def admin_dashboard(
    trading: TradingModule = Depends(deps.get_trading_service),
    position_mgr: PositionManager = Depends(deps.get_position_manager),
    signal_runtime: SignalRuntime = Depends(deps.get_signal_runtime),
    executor: TradeExecutor = Depends(deps.get_trade_executor),
    indicator_mgr: UnifiedIndicatorManager = Depends(deps.get_indicator_manager),
) -> ApiResponse[DashboardOverview]:
    """一次请求返回仪表板首屏所有核心数据。"""

    # ── system ──
    startup = deps.get_startup_status()
    started_at = startup.get("started_at")
    completed_at = startup.get("completed_at")
    uptime: Optional[float] = None
    if completed_at:
        try:
            t = datetime.fromisoformat(str(completed_at))
            uptime = (datetime.now(timezone.utc) - t).total_seconds()
        except (ValueError, TypeError):
            pass
    system = SystemStatusSnapshot(
        status="healthy" if startup.get("ready") else "starting",
        uptime_seconds=uptime,
        started_at=str(started_at) if started_at else None,
        ready=bool(startup.get("ready")),
        phase=str(startup.get("phase", "unknown")),
    )

    # ── account ──
    try:
        account_data: Dict[str, Any] = trading.health()
    except Exception:
        account_data = {"error": "unavailable"}

    # ── positions ──
    try:
        positions = position_mgr.active_positions()
        pos_data: Dict[str, Any] = {
            "count": len(positions),
            "items": positions[:20],
        }
    except Exception:
        pos_data = {"error": "unavailable"}

    # ── signals ──
    try:
        signals_data: Dict[str, Any] = signal_runtime.status()
    except Exception:
        signals_data = {"error": "unavailable"}

    # ── executor ──
    try:
        exec_status = executor.status()
        cb = exec_status.get("circuit_breaker", {})
        pending = exec_status.get("pending_entries") or {}
        executor_snap = ExecutorSnapshot(
            enabled=exec_status.get("enabled", False),
            circuit_open=cb.get("open", False),
            consecutive_failures=cb.get("consecutive_failures", 0),
            execution_count=exec_status.get("execution_count", 0),
            last_execution_at=exec_status.get("last_execution_at"),
            pending_entries_count=pending.get("active_count", 0),
        )
    except Exception:
        executor_snap = ExecutorSnapshot()

    # ── storage ──
    try:
        ingestor = deps.get_ingestor()
        storage_data: Dict[str, Any] = ingestor.queue_stats()
    except Exception:
        storage_data = {"error": "unavailable"}

    # ── indicators ──
    try:
        ind_data: Dict[str, Any] = indicator_mgr.get_performance_stats()
    except Exception:
        ind_data = {"error": "unavailable"}

    overview = DashboardOverview(
        system=system,
        account=account_data,
        positions=pos_data,
        signals=signals_data,
        executor=executor_snap,
        storage=storage_data,
        indicators=ind_data,
    )
    return ApiResponse.success_response(overview)


# ═══════════════════════════════════════════════════════════════
# 3.2  配置查看（只读）
# ═══════════════════════════════════════════════════════════════


@router.get("/config", response_model=ApiResponse[ConfigView])
def admin_config(
    section: Optional[str] = Query(
        default=None,
        description="按 section 过滤（trading / signal / risk / api 等）",
    ),
) -> ApiResponse[ConfigView]:
    """返回所有配置的聚合视图（含来源标注），敏感字段已由上游脱敏。"""
    # get_effective_config_snapshot() 内部已对 api_key、经济日历 API key 做了脱敏
    effective = get_effective_config_snapshot()
    provenance = get_config_provenance_snapshot()

    if section:
        effective = {k: v for k, v in effective.items() if k == section}
        provenance = {k: v for k, v in provenance.items() if k == section}

    return ApiResponse.success_response(
        ConfigView(
            effective=effective,
            provenance=provenance,
            files=list(_CONFIG_FILES),
        )
    )


@router.get("/config/signal", response_model=ApiResponse[Dict[str, Any]])
def admin_config_signal() -> ApiResponse[Dict[str, Any]]:
    """返回 signal.ini 的全部配置（SignalConfig 模型导出）。"""
    cfg = get_signal_config()
    data = cfg.model_dump()
    return ApiResponse.success_response(data)


@router.get("/config/risk", response_model=ApiResponse[Dict[str, Any]])
def admin_config_risk() -> ApiResponse[Dict[str, Any]]:
    """返回 risk.ini 的全部配置。"""
    cfg = get_risk_config()
    data = cfg.model_dump()
    return ApiResponse.success_response(data)


@router.get("/config/indicators", response_model=ApiResponse[Dict[str, Any]])
def admin_config_indicators(
    enabled_only: bool = Query(default=False, description="仅返回已启用指标"),
    indicator_mgr: UnifiedIndicatorManager = Depends(deps.get_indicator_manager),
) -> ApiResponse[Dict[str, Any]]:
    """返回 indicators.json 的结构化内容。"""
    indicators: List[Dict[str, Any]] = _load_json_config("indicators.json")

    if enabled_only:
        indicators = [ind for ind in indicators if ind.get("enabled", True)]

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
            "enabled_count": sum(1 for i in indicators if i.get("enabled", True)),
            "intrabar_indicators": intrabar_names,
        }
    )


@router.get("/config/composites", response_model=ApiResponse[List[Dict[str, Any]]])
def admin_config_composites() -> ApiResponse[List[Dict[str, Any]]]:
    """返回 composites.json 的复合策略定义。"""
    composites: List[Dict[str, Any]] = _load_json_config("composites.json")
    return ApiResponse.success_response(composites)


# ═══════════════════════════════════════════════════════════════
# 3.3  绩效报表
# ═══════════════════════════════════════════════════════════════


@router.get(
    "/performance/strategies",
    response_model=ApiResponse[StrategyPerformanceReport],
)
def admin_performance_strategies(
    hours: int = Query(
        default=168, ge=1, le=720, description="历史胜率查询范围（小时）"
    ),
    perf_tracker: StrategyPerformanceTracker = Depends(deps.get_performance_tracker),
    calibrator: ConfidenceCalibrator = Depends(deps.get_calibrator),
    signal_svc: SignalModule = Depends(deps.get_signal_service),
) -> ApiResponse[StrategyPerformanceReport]:
    """策略绩效排名 + 日内统计 + 历史胜率 + 校准器状态。"""
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
        cal_info = calibrator.describe()
    except Exception:
        cal_info = {}

    report = StrategyPerformanceReport(
        session_ranking=ranking,
        session_summary=summary,
        historical_winrates=winrates,
        calibrator=cal_info,
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
    """置信度管线可视化：展示每个策略从 raw 到 final 的置信度修正链路。"""
    # Regime 状态
    regime_info = signal_runtime.get_regime_stability(symbol, timeframe) or {}

    # 每个策略的管线信息
    strategies_pipeline: List[Dict[str, Any]] = []
    for name in signal_svc.list_strategies():
        affinity_map = signal_svc.strategy_affinity_map(name)
        affinity_dict = (
            {k.value: v for k, v in affinity_map.items()} if affinity_map else {}
        )

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
                "strategy": name,
                "regime_affinity": affinity_dict,
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


# ═══════════════════════════════════════════════════════════════
# 3.4  策略详情
# ═══════════════════════════════════════════════════════════════


@router.get("/strategies", response_model=ApiResponse[List[StrategyDetail]])
def admin_strategies(
    signal_svc: SignalModule = Depends(deps.get_signal_service),
) -> ApiResponse[List[StrategyDetail]]:
    """返回所有策略的完整信息（名称、类别、scope、指标、亲和度）。"""
    result: List[StrategyDetail] = []
    for name in signal_svc.list_strategies():
        result.append(_build_strategy_detail(name, signal_svc))
    return ApiResponse.success_response(result)


@router.get("/strategies/{name}", response_model=ApiResponse[Dict[str, Any]])
def admin_strategy_detail(
    name: str,
    signal_svc: SignalModule = Depends(deps.get_signal_service),
    perf_tracker: StrategyPerformanceTracker = Depends(deps.get_performance_tracker),
) -> ApiResponse[Any]:
    """返回单个策略的完整详情（属性 + 日内绩效）。"""
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


# ═══════════════════════════════════════════════════════════════
# 3.5  SSE 实时事件推送
# ═══════════════════════════════════════════════════════════════


@router.get("/events/stream")
async def admin_events_stream(
    scope: str = Query(
        default="all",
        pattern="^(confirmed|intrabar|all)$",
        description="过滤 scope（confirmed / intrabar / all）",
    ),
    symbol: Optional[str] = Query(default=None, description="按品种过滤"),
    signal_runtime: SignalRuntime = Depends(deps.get_signal_runtime),
) -> StreamingResponse:
    """SSE 端点：推送信号事件、心跳。"""
    queue: asyncio.Queue[Optional[Dict[str, Any]]] = asyncio.Queue(maxsize=256)
    loop = asyncio.get_running_loop()

    def _on_signal(event: SignalEvent) -> None:
        # 此回调在 SignalRuntime 的后台线程中执行，
        # 必须通过 call_soon_threadsafe 将数据投递到 asyncio 事件循环。
        if scope != "all" and event.scope != scope:
            return
        if symbol and event.symbol != symbol:
            return
        payload = {
            "type": "signal",
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
            pass  # 事件循环已关闭或队列满——best-effort

    signal_runtime.add_signal_listener(_on_signal)

    async def event_generator():  # type: ignore[no-untyped-def]
        heartbeat_interval = 30.0
        try:
            while True:
                try:
                    data = await asyncio.wait_for(
                        queue.get(), timeout=heartbeat_interval
                    )
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'heartbeat', 'ts': datetime.now(timezone.utc).isoformat()})}\n\n"
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
