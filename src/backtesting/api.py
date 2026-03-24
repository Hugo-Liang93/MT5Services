"""回测 FastAPI 路由（submit/query job 模式）。

路由前缀: /v1/backtest

Job 生命周期：
  submit → pending → running → completed / failed

存储：
- BacktestJob 元数据：内存 + DB write-through（API 重启后可恢复）
- BacktestResult 详情：DB 持久化（回测完成后写入）
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

from .models import BacktestJob, BacktestJobStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/backtest", tags=["backtest"])

# Job store: run_id → BacktestJob（内存 write-through，DB 持久化）
_job_store: Dict[str, BacktestJob] = {}
_job_lock = threading.Lock()

# 并发限制：同一时刻最多 1 个回测/优化任务运行，避免耗尽 DB 连接和 CPU
_backtest_semaphore = threading.Semaphore(1)

# 回测结果缓存（仅运行中和刚完成的任务，历史数据从 DB 查询）
_result_cache: Dict[str, Dict[str, Any]] = {}
_result_cache_lock = threading.Lock()

# Walk-Forward 结果缓存（WalkForwardResult 对象，不可序列化为 dict，独立缓存）
_wf_result_cache: Dict[str, Any] = {}
_wf_result_cache_lock = threading.Lock()


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


class WalkForwardRequest(BaseModel):
    symbol: str
    timeframe: str
    start_time: str = Field(..., description="ISO 格式起始时间")
    end_time: str = Field(..., description="ISO 格式结束时间")
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
    # Walk-Forward 专属参数
    n_splits: int = Field(default=5, description="滚动窗口数量")
    train_ratio: float = Field(default=0.7, description="训练集占比")
    anchored: bool = Field(default=False, description="是否使用锚定窗口")


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


class BacktestJobResponse(BaseModel):
    run_id: str
    status: str
    job_type: str = "backtest"
    submitted_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    progress: float = 0.0
    error: Optional[str] = None


# ── 路由实现 ────────────────────────────────────────────────────


@router.post("/run", response_model=ApiResponse)
async def run_backtest(
    request: BacktestRunRequest,
    background_tasks: BackgroundTasks,
) -> ApiResponse:
    """提交单次回测任务。"""
    run_id = f"bt_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    job = BacktestJob(
        run_id=run_id,
        job_type="backtest",
        status=BacktestJobStatus.PENDING,
        submitted_at=now,
        config_summary={
            "symbol": request.symbol,
            "timeframe": request.timeframe,
            "start_time": request.start_time,
            "end_time": request.end_time,
        },
    )
    _register_job(job)
    background_tasks.add_task(_execute_backtest, run_id, request)
    return ApiResponse(success=True, data=job.to_dict())


@router.post("/optimize", response_model=ApiResponse)
async def run_optimization(
    request: BacktestOptimizeRequest,
    background_tasks: BackgroundTasks,
) -> ApiResponse:
    """提交参数优化任务。"""
    run_id = f"opt_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    job = BacktestJob(
        run_id=run_id,
        job_type="optimization",
        status=BacktestJobStatus.PENDING,
        submitted_at=now,
        config_summary={
            "symbol": request.symbol,
            "timeframe": request.timeframe,
            "start_time": request.start_time,
            "end_time": request.end_time,
            "search_mode": request.search_mode,
            "max_combinations": request.max_combinations,
        },
    )
    _register_job(job)
    background_tasks.add_task(_execute_optimization, run_id, request)
    return ApiResponse(success=True, data=job.to_dict())


@router.post("/walk-forward", response_model=ApiResponse)
async def run_walk_forward(
    request: WalkForwardRequest,
    background_tasks: BackgroundTasks,
) -> ApiResponse:
    """提交 Walk-Forward 验证任务。"""
    run_id = f"wf_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    job = BacktestJob(
        run_id=run_id,
        job_type="walk_forward",
        status=BacktestJobStatus.PENDING,
        submitted_at=now,
        config_summary={
            "symbol": request.symbol,
            "timeframe": request.timeframe,
            "start_time": request.start_time,
            "end_time": request.end_time,
            "n_splits": request.n_splits,
            "train_ratio": request.train_ratio,
        },
    )
    _register_job(job)
    background_tasks.add_task(_execute_walk_forward, run_id, request)
    return ApiResponse(success=True, data=job.to_dict())


@router.get("/jobs", response_model=ApiResponse)
async def list_jobs() -> ApiResponse:
    """列出所有任务状态（内存中的活跃任务）。"""
    with _job_lock:
        jobs = [job.to_dict() for job in _job_store.values()]
    return ApiResponse(success=True, data=jobs)


@router.get("/jobs/{run_id}", response_model=ApiResponse)
async def get_job_status(run_id: str) -> ApiResponse:
    """查询任务状态。"""
    with _job_lock:
        job = _job_store.get(run_id)
    if job is not None:
        return ApiResponse(success=True, data=job.to_dict())
    return ApiResponse(success=False, error=f"Job {run_id} not found")


@router.get("/results/{run_id}", response_model=ApiResponse)
async def get_result(run_id: str) -> ApiResponse:
    """获取回测结果详情（优先内存缓存，回退 DB）。"""
    with _result_cache_lock:
        cached = _result_cache.get(run_id)
    if cached is not None:
        return ApiResponse(success=True, data=cached)

    # 回退到 DB 查询
    try:
        repo = _get_backtest_repo()
        if repo is not None:
            db_result = repo.fetch_run(run_id)
            if db_result is not None:
                return ApiResponse(success=True, data=db_result)
    except Exception:
        logger.debug("DB fallback query failed for %s", run_id, exc_info=True)

    # 检查 job 是否还在运行
    with _job_lock:
        job = _job_store.get(run_id)
    if job is not None:
        if job.status == BacktestJobStatus.RUNNING:
            return ApiResponse(
                success=True,
                data={"run_id": run_id, "status": "running", "progress": job.progress},
            )
        if job.status == BacktestJobStatus.FAILED:
            return ApiResponse(success=False, error=job.error or "Unknown error")
        if job.status == BacktestJobStatus.PENDING:
            return ApiResponse(
                success=True,
                data={"run_id": run_id, "status": "pending"},
            )

    return ApiResponse(success=False, error=f"Run {run_id} not found")


# 保留旧端点别名（向后兼容）
@router.get("/results", response_model=ApiResponse)
async def list_results() -> ApiResponse:
    """列出所有任务（兼容旧 API）。"""
    return await list_jobs()


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
        _fail_job(run_id, "另一个回测/优化任务正在执行，请稍后重试")
        return

    _start_job(run_id)
    components: Optional[Dict[str, Any]] = None
    try:
        from src.backtesting.engine import BacktestEngine
        from src.backtesting.models import BacktestConfig

        config = BacktestConfig(
            symbol=request.symbol,
            timeframe=request.timeframe,
            start_time=datetime.fromisoformat(request.start_time).replace(
                tzinfo=timezone.utc
            ),
            end_time=datetime.fromisoformat(request.end_time).replace(
                tzinfo=timezone.utc
            ),
            strategies=request.strategies,
            initial_balance=request.initial_balance,
            min_confidence=request.min_confidence,
            warmup_bars=request.warmup_bars,
            strategy_params=request.strategy_params,
            enable_filters=request.enable_filters,
            filter_allowed_sessions=request.filter_allowed_sessions,
            filter_session_transition_cooldown=request.filter_session_transition_cooldown,
            filter_volatility_spike_multiplier=request.filter_volatility_spike_multiplier,
        )

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

        _persist_result(result)
        _complete_job(run_id, result_dict)

    except Exception as e:
        logger.exception("Backtest %s failed", run_id)
        _fail_job(run_id, str(e))
    finally:
        _cleanup_components(components)
        _backtest_semaphore.release()


def _execute_optimization(run_id: str, request: BacktestOptimizeRequest) -> None:
    """在后台线程执行参数优化。"""
    acquired = _backtest_semaphore.acquire(timeout=5)
    if not acquired:
        _fail_job(run_id, "另一个回测/优化任务正在执行，请稍后重试")
        return

    _start_job(run_id)
    components: Optional[Dict[str, Any]] = None
    try:
        from src.backtesting.models import BacktestConfig, ParameterSpace
        from src.backtesting.optimizer import (
            ParameterOptimizer,
            build_signal_module_with_overrides,
        )

        config = BacktestConfig(
            symbol=request.symbol,
            timeframe=request.timeframe,
            start_time=datetime.fromisoformat(request.start_time).replace(
                tzinfo=timezone.utc
            ),
            end_time=datetime.fromisoformat(request.end_time).replace(
                tzinfo=timezone.utc
            ),
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
        for r in results:
            _persist_result(r)

        results_dicts = [r.to_dict() for r in results[:50]]
        _complete_job(run_id, results_dicts)

    except Exception as e:
        logger.exception("Optimization %s failed", run_id)
        _fail_job(run_id, str(e))
    finally:
        _cleanup_components(components)
        _backtest_semaphore.release()


def _execute_walk_forward(run_id: str, request: WalkForwardRequest) -> None:
    """在后台线程执行 Walk-Forward 验证。"""
    acquired = _backtest_semaphore.acquire(timeout=5)
    if not acquired:
        _fail_job(run_id, "另一个回测/优化任务正在执行，请稍后重试")
        return

    _start_job(run_id)
    components: Optional[Dict[str, Any]] = None
    try:
        from src.backtesting.models import BacktestConfig, ParameterSpace
        from src.backtesting.optimizer import build_signal_module_with_overrides
        from src.backtesting.walk_forward import WalkForwardConfig, WalkForwardValidator

        base_config = BacktestConfig(
            symbol=request.symbol,
            timeframe=request.timeframe,
            start_time=datetime.fromisoformat(request.start_time).replace(
                tzinfo=timezone.utc
            ),
            end_time=datetime.fromisoformat(request.end_time).replace(
                tzinfo=timezone.utc
            ),
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

        wf_config = WalkForwardConfig(
            total_start_time=base_config.start_time,
            total_end_time=base_config.end_time,
            base_config=base_config,
            train_ratio=request.train_ratio,
            n_splits=request.n_splits,
            anchored=request.anchored,
            optimization_metric=request.sort_metric,
            param_space=param_space,
        )

        components = _build_api_components()
        base_module = components["signal_module"]

        def module_factory(params: Dict[str, Any]) -> Any:
            return build_signal_module_with_overrides(base_module, params)

        validator = WalkForwardValidator(
            config=wf_config,
            data_loader=components["data_loader"],
            signal_module_factory=module_factory,
            indicator_pipeline=components["pipeline"],
            regime_detector=components["regime_detector"],
            voting_engine=components.get("voting_engine"),
        )

        wf_result = validator.run()

        # 缓存 WalkForwardResult 对象（供推荐引擎使用）
        with _wf_result_cache_lock:
            _wf_result_cache[run_id] = wf_result

        # 持久化各窗口的 OOS 结果
        for split in wf_result.splits:
            _persist_result(split.out_of_sample_result)

        # 序列化摘要给 API 查询
        summary = {
            "run_id": run_id,
            "n_splits": len(wf_result.splits),
            "overfitting_ratio": round(wf_result.overfitting_ratio, 4),
            "consistency_rate": round(wf_result.consistency_rate, 4),
            "aggregate_metrics": {
                "total_trades": wf_result.aggregate_metrics.total_trades,
                "win_rate": wf_result.aggregate_metrics.win_rate,
                "sharpe_ratio": wf_result.aggregate_metrics.sharpe_ratio,
                "max_drawdown": wf_result.aggregate_metrics.max_drawdown,
                "total_pnl": wf_result.aggregate_metrics.total_pnl,
                "profit_factor": wf_result.aggregate_metrics.profit_factor,
            },
            "splits": [
                {
                    "split_index": s.split_index,
                    "best_params": s.best_params,
                    "in_sample_sharpe": s.in_sample_result.metrics.sharpe_ratio,
                    "out_of_sample_sharpe": s.out_of_sample_result.metrics.sharpe_ratio,
                }
                for s in wf_result.splits
            ],
        }
        _complete_job(run_id, summary)

    except Exception as e:
        logger.exception("Walk-Forward %s failed", run_id)
        _fail_job(run_id, str(e))
    finally:
        _cleanup_components(components)
        _backtest_semaphore.release()


# ── Job 状态管理 ─────────────────────────────────────────────────


def _register_job(job: BacktestJob) -> None:
    """注册新任务到内存 store。"""
    with _job_lock:
        _job_store[job.run_id] = job


def _start_job(run_id: str) -> None:
    """标记任务开始执行。"""
    with _job_lock:
        job = _job_store.get(run_id)
        if job is not None:
            job.status = BacktestJobStatus.RUNNING
            job.started_at = datetime.now(timezone.utc)


def _complete_job(run_id: str, result: Any) -> None:
    """标记任务完成并缓存结果。"""
    now = datetime.now(timezone.utc)
    with _job_lock:
        job = _job_store.get(run_id)
        if job is not None:
            job.status = BacktestJobStatus.COMPLETED
            job.completed_at = now
            job.progress = 1.0
    with _result_cache_lock:
        _result_cache[run_id] = result


def _fail_job(run_id: str, error: str) -> None:
    """标记任务失败。"""
    now = datetime.now(timezone.utc)
    with _job_lock:
        job = _job_store.get(run_id)
        if job is not None:
            job.status = BacktestJobStatus.FAILED
            job.completed_at = now
            job.error = error


# ── 组件管理 ──────────────────────────────────────────────────────


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
        logger.warning(
            "Failed to persist backtest result %s", result.run_id, exc_info=True
        )


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


# ── 参数推荐 API ─────────────────────────────────────────────────────

# 内存缓存（rec_id → Recommendation），DB write-through
_rec_cache: Dict[str, Any] = {}
_rec_cache_lock = threading.Lock()


class GenerateRecommendationRequest(BaseModel):
    walk_forward_run_id: str = Field(..., description="Walk-Forward 验证的 run_id")


class ApproveRejectRequest(BaseModel):
    reason: str = Field(default="", description="审核理由（可选）")


@router.post("/recommendations/generate", response_model=ApiResponse)
async def generate_recommendation(
    request: GenerateRecommendationRequest,
) -> ApiResponse:
    """从 Walk-Forward 验证结果生成参数推荐。

    前置条件：Walk-Forward 验证已完成且结果可查。
    """
    try:
        wf_result = _load_walk_forward_result(request.walk_forward_run_id)
        if wf_result is None:
            return ApiResponse(
                success=False,
                error=f"Walk-Forward 结果 {request.walk_forward_run_id} 未找到",
            )

        from src.backtesting.recommendation import (
            RecommendationEngine,
            load_current_signal_config,
        )

        current_params, current_affinities = load_current_signal_config()
        engine = RecommendationEngine()
        rec = engine.generate(
            wf_result=wf_result,
            source_run_id=request.walk_forward_run_id,
            current_strategy_params=current_params,
            current_regime_affinities=current_affinities,
        )

        # 持久化
        repo = _get_backtest_repo()
        if repo is not None:
            repo.save_recommendation(rec)

        with _rec_cache_lock:
            _rec_cache[rec.rec_id] = rec

        return ApiResponse(success=True, data=rec.to_dict())

    except ValueError as e:
        return ApiResponse(success=False, error=str(e))
    except Exception as e:
        logger.exception("Failed to generate recommendation")
        return ApiResponse(success=False, error=str(e))


@router.get("/recommendations", response_model=ApiResponse)
async def list_recommendations(
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> ApiResponse:
    """列出参数推荐记录（支持 status 筛选）。"""
    try:
        repo = _get_backtest_repo()
        if repo is None:
            return ApiResponse(success=False, error="Database not available")
        recs = repo.fetch_recommendations(status=status, limit=limit, offset=offset)
        return ApiResponse(success=True, data=[r.to_dict() for r in recs])
    except Exception as e:
        return ApiResponse(success=False, error=str(e))


@router.get("/recommendations/{rec_id}", response_model=ApiResponse)
async def get_recommendation(rec_id: str) -> ApiResponse:
    """查看推荐详情（含参数 diff）。"""
    rec = _get_recommendation(rec_id)
    if rec is None:
        return ApiResponse(success=False, error=f"推荐 {rec_id} 未找到")
    return ApiResponse(success=True, data=rec.to_dict())


@router.post("/recommendations/{rec_id}/approve", response_model=ApiResponse)
async def approve_recommendation(
    rec_id: str,
    request: ApproveRejectRequest,
) -> ApiResponse:
    """审核通过参数推荐。"""
    from src.backtesting.models import RecommendationStatus

    rec = _get_recommendation(rec_id)
    if rec is None:
        return ApiResponse(success=False, error=f"推荐 {rec_id} 未找到")
    if rec.status != RecommendationStatus.PENDING:
        return ApiResponse(
            success=False,
            error=f"推荐状态为 {rec.status.value}，仅 pending 可审核",
        )

    rec.status = RecommendationStatus.APPROVED
    rec.approved_at = datetime.now(timezone.utc)

    repo = _get_backtest_repo()
    if repo is not None:
        repo.update_recommendation(rec)

    with _rec_cache_lock:
        _rec_cache[rec_id] = rec

    return ApiResponse(success=True, data=rec.to_dict())


@router.post("/recommendations/{rec_id}/reject", response_model=ApiResponse)
async def reject_recommendation(
    rec_id: str,
    request: ApproveRejectRequest,
) -> ApiResponse:
    """审核拒绝参数推荐。"""
    from src.backtesting.models import RecommendationStatus

    rec = _get_recommendation(rec_id)
    if rec is None:
        return ApiResponse(success=False, error=f"推荐 {rec_id} 未找到")
    if rec.status != RecommendationStatus.PENDING:
        return ApiResponse(
            success=False,
            error=f"推荐状态为 {rec.status.value}，仅 pending 可拒绝",
        )

    rec.status = RecommendationStatus.REJECTED
    repo = _get_backtest_repo()
    if repo is not None:
        repo.update_recommendation(rec)

    with _rec_cache_lock:
        _rec_cache[rec_id] = rec

    return ApiResponse(success=True, data=rec.to_dict())


@router.post("/recommendations/{rec_id}/apply", response_model=ApiResponse)
async def apply_recommendation(rec_id: str) -> ApiResponse:
    """应用已审核通过的推荐（写入 signal.local.ini + 内存热更新）。"""
    from src.backtesting.recommendation import ConfigApplicator

    rec = _get_recommendation(rec_id)
    if rec is None:
        return ApiResponse(success=False, error=f"推荐 {rec_id} 未找到")

    try:
        signal_module = _get_signal_module()
        applicator = ConfigApplicator(signal_module=signal_module)
        backup_path = applicator.apply(rec)

        # DB 持久化（失败时记录警告但不回滚文件操作——
        # 配置已生效，DB 状态可通过下次查询时自愈）
        repo = _get_backtest_repo()
        if repo is not None:
            try:
                repo.update_recommendation(rec)
            except Exception:
                logger.warning(
                    "推荐 %s 已应用但 DB 更新失败，状态可能不一致",
                    rec_id,
                    exc_info=True,
                )

        with _rec_cache_lock:
            _rec_cache[rec_id] = rec

        return ApiResponse(
            success=True,
            data={**rec.to_dict(), "backup_path": backup_path},
        )
    except ValueError as e:
        return ApiResponse(success=False, error=str(e))
    except Exception as e:
        logger.exception("Failed to apply recommendation %s", rec_id)
        return ApiResponse(success=False, error=str(e))


@router.post("/recommendations/{rec_id}/rollback", response_model=ApiResponse)
async def rollback_recommendation(rec_id: str) -> ApiResponse:
    """回滚已应用的推荐。"""
    from src.backtesting.recommendation import ConfigApplicator

    rec = _get_recommendation(rec_id)
    if rec is None:
        return ApiResponse(success=False, error=f"推荐 {rec_id} 未找到")

    try:
        signal_module = _get_signal_module()
        applicator = ConfigApplicator(signal_module=signal_module)
        applicator.rollback(rec)

        repo = _get_backtest_repo()
        if repo is not None:
            try:
                repo.update_recommendation(rec)
            except Exception:
                logger.warning(
                    "推荐 %s 已回滚但 DB 更新失败",
                    rec_id,
                    exc_info=True,
                )

        with _rec_cache_lock:
            _rec_cache[rec_id] = rec

        return ApiResponse(success=True, data=rec.to_dict())
    except ValueError as e:
        return ApiResponse(success=False, error=str(e))
    except Exception as e:
        logger.exception("Failed to rollback recommendation %s", rec_id)
        return ApiResponse(success=False, error=str(e))


# ── 推荐 helper ──────────────────────────────────────────────────────


def _get_recommendation(rec_id: str) -> Optional[Any]:
    """获取推荐记录（优先内存缓存，回退 DB）。"""
    with _rec_cache_lock:
        cached = _rec_cache.get(rec_id)
    if cached is not None:
        return cached

    repo = _get_backtest_repo()
    if repo is not None:
        rec = repo.fetch_recommendation(rec_id)
        if rec is not None:
            with _rec_cache_lock:
                _rec_cache[rec_id] = rec
            return rec
    return None


def _load_walk_forward_result(run_id: str) -> Optional[Any]:
    """从 Walk-Forward 专用缓存加载结果。

    WalkForwardResult 对象（含 splits、BacktestResult 等）无法从 DB 重建，
    因此仅在当前进程内有效。API 重启后需重新运行 Walk-Forward 验证。
    """
    with _wf_result_cache_lock:
        return _wf_result_cache.get(run_id)


def _get_signal_module() -> Optional[Any]:
    """尝试获取运行时的 SignalModule 实例。"""
    try:
        from src.api.deps import get_signal_service

        return get_signal_service()
    except Exception:
        logger.debug("SignalModule not available (standalone backtest mode)")
        return None
