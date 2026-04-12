from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, BackgroundTasks

from src.api.schemas import ApiResponse
from src.api.backtest_routes import schemas as api_config, execution as api_execution
from src.backtesting.models import BacktestJob, BacktestJobStatus
from src.backtesting.data import backtest_runtime_store

router = APIRouter()
logger = logging.getLogger(__name__)


def _resolve_job_simulation_mode(request: api_config.BacktestRequestBase) -> str:
    if request.simulation_mode:
        return str(request.simulation_mode)
    defaults = api_config.load_backtest_defaults()
    return str(defaults.get("simulation_mode", "research"))


def _create_job(
    *,
    run_id: str,
    job_type: str,
    config_summary: Dict[str, Any],
) -> BacktestJob:
    job = BacktestJob(
        run_id=run_id,
        job_type=job_type,
        status=BacktestJobStatus.PENDING,
        submitted_at=datetime.now(timezone.utc),
        config_summary=config_summary,
    )
    backtest_runtime_store.register_job(job)
    return job


@router.post("/run", response_model=ApiResponse)
async def run_backtest(
    request: api_config.BacktestRunRequest,
    background_tasks: BackgroundTasks,
) -> ApiResponse:
    run_id = f"bt_{uuid.uuid4().hex[:12]}"
    job = _create_job(
        run_id=run_id,
        job_type="backtest",
        config_summary={
            "symbol": request.symbol,
            "timeframe": request.timeframe,
            "start_time": request.start_time,
            "end_time": request.end_time,
            "simulation_mode": _resolve_job_simulation_mode(request),
        },
    )
    background_tasks.add_task(api_execution.execute_backtest, run_id, request)
    return ApiResponse(success=True, data=job.to_dict())


@router.post("/optimize", response_model=ApiResponse)
async def run_optimization(
    request: api_config.BacktestOptimizeRequest,
    background_tasks: BackgroundTasks,
) -> ApiResponse:
    optimizer_settings = api_config.resolve_optimizer_settings(request)
    run_id = f"opt_{uuid.uuid4().hex[:12]}"
    job = _create_job(
        run_id=run_id,
        job_type="optimization",
        config_summary={
            "symbol": request.symbol,
            "timeframe": request.timeframe,
            "start_time": request.start_time,
            "end_time": request.end_time,
            "simulation_mode": _resolve_job_simulation_mode(request),
            "search_mode": optimizer_settings["search_mode"],
            "max_combinations": optimizer_settings["max_combinations"],
        },
    )
    background_tasks.add_task(api_execution.execute_optimization, run_id, request)
    return ApiResponse(success=True, data=job.to_dict())


@router.post("/walk-forward", response_model=ApiResponse)
async def run_walk_forward(
    request: api_config.WalkForwardRequest,
    background_tasks: BackgroundTasks,
) -> ApiResponse:
    optimizer_settings = api_config.resolve_optimizer_settings(request)
    run_id = f"wf_{uuid.uuid4().hex[:12]}"
    job = _create_job(
        run_id=run_id,
        job_type="walk_forward",
        config_summary={
            "symbol": request.symbol,
            "timeframe": request.timeframe,
            "start_time": request.start_time,
            "end_time": request.end_time,
            "simulation_mode": _resolve_job_simulation_mode(request),
            "n_splits": request.n_splits,
            "train_ratio": request.train_ratio,
            "search_mode": optimizer_settings["search_mode"],
            "max_combinations": optimizer_settings["max_combinations"],
        },
    )
    background_tasks.add_task(api_execution.execute_walk_forward, run_id, request)
    return ApiResponse(success=True, data=job.to_dict())


@router.get("/jobs", response_model=ApiResponse)
async def list_jobs() -> ApiResponse:
    return ApiResponse(
        success=True,
        data=[job.to_dict() for job in backtest_runtime_store.list_jobs()],
    )


@router.get("/jobs/{run_id}", response_model=ApiResponse)
async def get_job_status(run_id: str) -> ApiResponse:
    job = backtest_runtime_store.get_job(run_id)
    if job is not None:
        return ApiResponse(success=True, data=job.to_dict())
    return ApiResponse(success=False, error=f"Job {run_id} not found")


@router.get("/results/{run_id}", response_model=ApiResponse)
async def get_result(run_id: str) -> ApiResponse:
    cached = backtest_runtime_store.get_result(run_id)
    if cached is not None:
        return ApiResponse(success=True, data=cached)

    try:
        repo = api_execution.get_backtest_repo()
        if repo is not None:
            db_result = repo.fetch_run(run_id)
            if db_result is not None:
                return ApiResponse(success=True, data=db_result)
    except Exception:
        logger.debug("DB fallback query failed for %s", run_id, exc_info=True)

    job = backtest_runtime_store.get_job(run_id)
    if job is not None:
        if job.status == BacktestJobStatus.RUNNING:
            return ApiResponse(
                success=True,
                data={"run_id": run_id, "status": "running", "progress": job.progress},
            )
        if job.status == BacktestJobStatus.FAILED:
            return ApiResponse(success=False, error=job.error or "Unknown error")
        if job.status == BacktestJobStatus.PENDING:
            return ApiResponse(success=True, data={"run_id": run_id, "status": "pending"})

    return ApiResponse(success=False, error=f"Run {run_id} not found")


@router.get("/results", response_model=ApiResponse)
async def list_results() -> ApiResponse:
    summaries: list[Dict[str, Any]] = []
    for job in backtest_runtime_store.list_jobs():
        entry: Dict[str, Any] = {
            "run_id": job.run_id,
            "type": job.job_type,
            "status": job.status.value,
            "submitted_at": job.submitted_at.isoformat(),
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            "config_summary": job.config_summary,
        }
        cached = backtest_runtime_store.get_result(job.run_id)
        if cached is not None:
            entry["metrics"] = api_execution.extract_result_metrics(cached, job.job_type)
        summaries.append(entry)
    return ApiResponse(success=True, data=summaries)


@router.get("/history", response_model=ApiResponse)
async def list_history(limit: int = 50, offset: int = 0) -> ApiResponse:
    try:
        repo = api_execution.get_backtest_repo()
        if repo is None:
            return ApiResponse(success=False, error="Database not available")
        runs = repo.fetch_runs(limit=limit, offset=offset)
        return ApiResponse(success=True, data=runs)
    except Exception as exc:
        return ApiResponse(success=False, error=str(exc))


@router.get("/history/{run_id}/trades", response_model=ApiResponse)
async def get_trades(run_id: str) -> ApiResponse:
    try:
        repo = api_execution.get_backtest_repo()
        if repo is None:
            return ApiResponse(success=False, error="Database not available")
        return ApiResponse(success=True, data=repo.fetch_trades(run_id))
    except Exception as exc:
        return ApiResponse(success=False, error=str(exc))


@router.get("/history/{run_id}/evaluations", response_model=ApiResponse)
async def get_evaluations(
    run_id: str,
    strategy: str | None = None,
    filtered_only: bool = False,
    limit: int = 1000,
) -> ApiResponse:
    try:
        repo = api_execution.get_backtest_repo()
        if repo is None:
            return ApiResponse(success=False, error="Database not available")
        evaluations = repo.fetch_evaluations(
            run_id,
            strategy=strategy,
            filtered_only=filtered_only,
            limit=limit,
        )
        return ApiResponse(success=True, data=evaluations)
    except Exception as exc:
        return ApiResponse(success=False, error=str(exc))


@router.get("/history/{run_id}/evaluation-summary", response_model=ApiResponse)
async def get_evaluation_summary(run_id: str) -> ApiResponse:
    try:
        repo = api_execution.get_backtest_repo()
        if repo is None:
            return ApiResponse(success=False, error="Database not available")
        summary = repo.fetch_evaluation_summary(run_id)
        return ApiResponse(success=True, data=summary)
    except Exception as exc:
        return ApiResponse(success=False, error=str(exc))


@router.post(
    "/results/{run_id}/correlation-analysis",
    response_model=ApiResponse[dict],
    summary="对回测结果运行策略相关性分析",
)
async def run_correlation_analysis(
    run_id: str,
    correlation_threshold: float = 0.80,
    penalty_weight: float = 0.50,
) -> ApiResponse[dict]:
    result = backtest_runtime_store.get_result(run_id)
    if result is None:
        return ApiResponse.error_response(
            f"Result {run_id} not found", error_code="NOT_FOUND",
        )
    evaluations = getattr(result, "signal_evaluations", None)
    if not evaluations:
        return ApiResponse.error_response(
            "No signal evaluations in this result (run with max_signal_evaluations > 0)",
            error_code="NO_DATA",
        )
    from src.backtesting.analysis.correlation import (
        analyze_strategy_correlation,
        extract_signal_directions_from_evaluations,
    )

    directions = extract_signal_directions_from_evaluations(evaluations)
    if len(directions) < 2:
        return ApiResponse.error_response(
            "Need at least 2 strategies with signals for correlation analysis",
            error_code="INSUFFICIENT_DATA",
        )
    analysis = analyze_strategy_correlation(
        directions,
        correlation_threshold=correlation_threshold,
        penalty_weight=penalty_weight,
    )
    return ApiResponse.success_response(
        data=analysis.to_dict(),
        metadata={"run_id": run_id, "strategies_analyzed": len(directions)},
    )
