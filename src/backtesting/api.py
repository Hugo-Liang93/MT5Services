"""回测 FastAPI 路由。

路由前缀: /v1/backtest

支持两种结果存储模式：
1. 内存缓存（即时查询运行状态）
2. TimescaleDB 持久化（历史回测结果永久保存 + 信号评估明细）
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel, Field

from src.api.schemas import ApiResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/backtest", tags=["backtest"])

# 简易内存结果存储（用于查询运行状态，DB 持久化在回测完成后自动执行）
_results_store: Dict[str, Dict[str, Any]] = {}
_results_lock = threading.Lock()

# 并发限制：同一时刻最多 1 个回测/优化任务运行，避免耗尽 DB 连接和 CPU
_backtest_semaphore = threading.Semaphore(1)


# ── Request / Response Schemas ──────────────────────────────────


class BacktestRunRequest(BaseModel):
    symbol: str
    timeframe: str
    start_time: str = Field(..., description="ISO 格式起始时间 (YYYY-MM-DD)")
    end_time: str = Field(..., description="ISO 格式结束时间 (YYYY-MM-DD)")
    strategies: Optional[List[str]] = None
    initial_balance: float = 10000.0
    min_confidence: float = 0.55
    warmup_bars: int = 200
    strategy_params: Dict[str, Any] = Field(default_factory=dict)
    # 过滤器配置
    enable_filters: bool = True
    filter_allowed_sessions: str = "london,newyork"
    filter_session_transition_cooldown: int = 15
    filter_volatility_spike_multiplier: float = 2.5


class BacktestOptimizeRequest(BaseModel):
    symbol: str
    timeframe: str
    start_time: str
    end_time: str
    strategies: Optional[List[str]] = None
    initial_balance: float = 10000.0
    min_confidence: float = 0.55
    warmup_bars: int = 200
    param_space: Dict[str, List[Any]] = Field(
        ..., description="参数搜索空间 {key: [val1, val2, ...]}"
    )
    search_mode: str = "grid"
    max_combinations: int = 500
    sort_metric: str = "sharpe_ratio"


class BacktestStatusResponse(BaseModel):
    run_id: str
    status: str  # "running" | "completed" | "failed"
    message: Optional[str] = None


# ── 路由实现 ────────────────────────────────────────────────────


@router.post("/run", response_model=ApiResponse)
async def run_backtest(
    request: BacktestRunRequest,
    background_tasks: BackgroundTasks,
) -> ApiResponse:
    """触发单次回测（异步执行）。"""
    run_id = f"bt_{uuid.uuid4().hex[:12]}"

    with _results_lock:
        _results_store[run_id] = {"status": "running", "result": None, "error": None}

    background_tasks.add_task(_execute_backtest, run_id, request)

    return ApiResponse(
        success=True,
        data={"run_id": run_id, "status": "running"},
    )


@router.post("/optimize", response_model=ApiResponse)
async def run_optimization(
    request: BacktestOptimizeRequest,
    background_tasks: BackgroundTasks,
) -> ApiResponse:
    """触发参数优化（异步执行）。"""
    run_id = f"opt_{uuid.uuid4().hex[:12]}"

    with _results_lock:
        _results_store[run_id] = {"status": "running", "result": None, "error": None}

    background_tasks.add_task(_execute_optimization, run_id, request)

    return ApiResponse(
        success=True,
        data={"run_id": run_id, "status": "running"},
    )


@router.get("/results", response_model=ApiResponse)
async def list_results() -> ApiResponse:
    """列出所有回测结果（内存中的运行状态）。"""
    with _results_lock:
        summaries = []
        for run_id, entry in _results_store.items():
            summary: Dict[str, Any] = {"run_id": run_id, "status": entry["status"]}
            if entry["result"] is not None:
                if isinstance(entry["result"], list):
                    # 优化结果
                    summary["type"] = "optimization"
                    summary["count"] = len(entry["result"])
                else:
                    summary["type"] = "backtest"
                    summary["metrics"] = _extract_metrics(entry["result"])
            summaries.append(summary)

    return ApiResponse(success=True, data=summaries)


@router.get("/results/{run_id}", response_model=ApiResponse)
async def get_result(run_id: str) -> ApiResponse:
    """获取单次回测详情（优先内存，回退 DB）。"""
    with _results_lock:
        entry = _results_store.get(run_id)

    if entry is not None:
        if entry["status"] == "running":
            return ApiResponse(
                success=True,
                data={"run_id": run_id, "status": "running"},
            )
        if entry["status"] == "failed":
            return ApiResponse(
                success=False,
                error=entry.get("error", "Unknown error"),
            )
        return ApiResponse(success=True, data=entry["result"])

    # 回退到 DB 查询
    try:
        repo = _get_backtest_repo()
        if repo is not None:
            db_result = repo.fetch_run(run_id)
            if db_result is not None:
                return ApiResponse(success=True, data=db_result)
    except Exception:
        logger.debug("DB fallback query failed for %s", run_id, exc_info=True)

    return ApiResponse(success=False, error=f"Run {run_id} not found")


@router.get("/history", response_model=ApiResponse)
async def list_history(limit: int = 50, offset: int = 0) -> ApiResponse:
    """查询历史回测结果列表（从 DB）。"""
    try:
        repo = _get_backtest_repo()
        if repo is None:
            return ApiResponse(success=False, error="Database not available")
        runs = repo.fetch_runs(limit=limit, offset=offset)
        return ApiResponse(success=True, data=runs)
    except Exception as e:
        return ApiResponse(success=False, error=str(e))


@router.get("/history/{run_id}/trades", response_model=ApiResponse)
async def get_trades(run_id: str) -> ApiResponse:
    """查询某次回测的交易明细（从 DB）。"""
    try:
        repo = _get_backtest_repo()
        if repo is None:
            return ApiResponse(success=False, error="Database not available")
        trades = repo.fetch_trades(run_id)
        return ApiResponse(success=True, data=trades)
    except Exception as e:
        return ApiResponse(success=False, error=str(e))


@router.get("/history/{run_id}/evaluations", response_model=ApiResponse)
async def get_evaluations(
    run_id: str,
    strategy: Optional[str] = None,
    filtered_only: bool = False,
    limit: int = 1000,
) -> ApiResponse:
    """查询信号评估明细（从 DB）。"""
    try:
        repo = _get_backtest_repo()
        if repo is None:
            return ApiResponse(success=False, error="Database not available")
        evals = repo.fetch_evaluations(
            run_id, strategy=strategy, filtered_only=filtered_only, limit=limit
        )
        return ApiResponse(success=True, data=evals)
    except Exception as e:
        return ApiResponse(success=False, error=str(e))


@router.get("/history/{run_id}/evaluation-summary", response_model=ApiResponse)
async def get_evaluation_summary(run_id: str) -> ApiResponse:
    """查询信号评估汇总统计（从 DB）。"""
    try:
        repo = _get_backtest_repo()
        if repo is None:
            return ApiResponse(success=False, error="Database not available")
        summary = repo.fetch_evaluation_summary(run_id)
        return ApiResponse(success=True, data=summary)
    except Exception as e:
        return ApiResponse(success=False, error=str(e))


# ── 后台执行函数 ────────────────────────────────────────────────


def _execute_backtest(run_id: str, request: BacktestRunRequest) -> None:
    """在后台线程执行回测。"""
    acquired = _backtest_semaphore.acquire(timeout=5)
    if not acquired:
        with _results_lock:
            _results_store[run_id] = {
                "status": "failed",
                "result": None,
                "error": "另一个回测/优化任务正在执行，请稍后重试",
            }
        return

    components: Optional[Dict[str, Any]] = None
    try:
        from src.backtesting.models import BacktestConfig
        from src.backtesting.engine import BacktestEngine

        config = BacktestConfig(
            symbol=request.symbol,
            timeframe=request.timeframe,
            start_time=datetime.fromisoformat(request.start_time).replace(tzinfo=timezone.utc),
            end_time=datetime.fromisoformat(request.end_time).replace(tzinfo=timezone.utc),
            strategies=request.strategies,
            initial_balance=request.initial_balance,
            min_confidence=request.min_confidence,
            warmup_bars=request.warmup_bars,
            strategy_params=request.strategy_params,
            # 过滤器配置
            enable_filters=request.enable_filters,
            filter_allowed_sessions=request.filter_allowed_sessions,
            filter_session_transition_cooldown=request.filter_session_transition_cooldown,
            filter_volatility_spike_multiplier=request.filter_volatility_spike_multiplier,
        )

        # 构建组件（独立 pipeline + 独立 DB 连接池）
        components = _build_api_components(
            strategy_params=request.strategy_params or None,
        )
        engine = BacktestEngine(
            config=config,
            data_loader=components["data_loader"],
            signal_module=components["signal_module"],
            indicator_pipeline=components["pipeline"],
            regime_detector=components["regime_detector"],
            voting_engine=components.get("voting_engine"),
        )

        result = engine.run()
        result_dict = result.to_dict()

        # 持久化到 DB
        _persist_result(result)

        with _results_lock:
            _results_store[run_id] = {
                "status": "completed",
                "result": result_dict,
                "error": None,
            }

    except Exception as e:
        logger.exception("Backtest %s failed", run_id)
        with _results_lock:
            _results_store[run_id] = {
                "status": "failed",
                "result": None,
                "error": str(e),
            }
    finally:
        _cleanup_components(components)
        _backtest_semaphore.release()


def _execute_optimization(run_id: str, request: BacktestOptimizeRequest) -> None:
    """在后台线程执行参数优化。"""
    acquired = _backtest_semaphore.acquire(timeout=5)
    if not acquired:
        with _results_lock:
            _results_store[run_id] = {
                "status": "failed",
                "result": None,
                "error": "另一个回测/优化任务正在执行，请稍后重试",
            }
        return

    components: Optional[Dict[str, Any]] = None
    try:
        from src.backtesting.models import BacktestConfig, ParameterSpace
        from src.backtesting.optimizer import ParameterOptimizer, build_signal_module_with_overrides

        config = BacktestConfig(
            symbol=request.symbol,
            timeframe=request.timeframe,
            start_time=datetime.fromisoformat(request.start_time).replace(tzinfo=timezone.utc),
            end_time=datetime.fromisoformat(request.end_time).replace(tzinfo=timezone.utc),
            strategies=request.strategies,
            initial_balance=request.initial_balance,
            min_confidence=request.min_confidence,
            warmup_bars=request.warmup_bars,
        )

        param_space = ParameterSpace(
            strategy_params=request.param_space,
            search_mode=request.search_mode,
            max_combinations=request.max_combinations,
        )

        components = _build_api_components()
        base_module = components["signal_module"]

        def module_factory(params: Dict[str, Any]) -> Any:
            return build_signal_module_with_overrides(base_module, params)

        optimizer = ParameterOptimizer(
            base_config=config,
            param_space=param_space,
            data_loader=components["data_loader"],
            indicator_pipeline=components["pipeline"],
            signal_module_factory=module_factory,
            regime_detector=components["regime_detector"],
            voting_engine=components.get("voting_engine"),
            sort_metric=request.sort_metric,
        )

        results = optimizer.run()

        # 持久化所有优化结果
        for r in results:
            _persist_result(r)

        results_dicts = [r.to_dict() for r in results[:50]]  # 最多返回 50 组

        with _results_lock:
            _results_store[run_id] = {
                "status": "completed",
                "result": results_dicts,
                "error": None,
            }

    except Exception as e:
        logger.exception("Optimization %s failed", run_id)
        with _results_lock:
            _results_store[run_id] = {
                "status": "failed",
                "result": None,
                "error": str(e),
            }
    finally:
        _cleanup_components(components)
        _backtest_semaphore.release()


def _cleanup_components(components: Optional[Dict[str, Any]]) -> None:
    """回测完成后释放独立 pipeline 和 DB 连接资源。"""
    if components is None:
        return
    pipeline = components.get("pipeline")
    if pipeline is not None:
        try:
            pipeline.shutdown()
        except Exception:
            logger.debug("Pipeline shutdown error", exc_info=True)
    writer = components.get("writer")
    if writer is not None:
        try:
            writer.close()
        except Exception:
            logger.debug("Writer close error", exc_info=True)


def _persist_result(result: Any) -> None:
    """将回测结果持久化到 DB（best-effort，失败不影响主流程）。"""
    try:
        repo = _get_backtest_repo()
        if repo is not None:
            repo.save_result(result)
    except Exception:
        logger.warning("Failed to persist backtest result %s", result.run_id, exc_info=True)


_cached_backtest_repo: Optional[Any] = None


def _get_backtest_repo() -> Optional[Any]:
    """获取 BacktestRepository 实例（懒加载 + 模块级缓存，独立连接池）。"""
    global _cached_backtest_repo
    if _cached_backtest_repo is not None:
        return _cached_backtest_repo
    try:
        from src.config.database import get_db_config
        from src.persistence.db import TimescaleWriter
        from src.persistence.repositories.backtest_repo import BacktestRepository

        db_config = get_db_config()
        writer = TimescaleWriter(settings=db_config, min_conn=1, max_conn=2)
        repo = BacktestRepository(writer)
        repo.ensure_schema()
        _cached_backtest_repo = repo
        return repo
    except Exception:
        logger.debug("BacktestRepository not available", exc_info=True)
        return None


def _build_api_components(
    strategy_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构建回测所需组件（委托给共享工厂）。"""
    from .component_factory import build_backtest_components

    return build_backtest_components(strategy_params=strategy_params)


def _extract_metrics(result_dict: Dict[str, Any]) -> Dict[str, Any]:
    """提取简化的指标摘要。"""
    m = result_dict.get("metrics", {})
    return {
        "total_trades": m.get("total_trades", 0),
        "win_rate": m.get("win_rate", 0),
        "sharpe_ratio": m.get("sharpe_ratio", 0),
        "total_pnl": m.get("total_pnl", 0),
        "max_drawdown": m.get("max_drawdown", 0),
    }
