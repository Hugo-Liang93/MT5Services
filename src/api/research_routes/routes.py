"""Research 信号挖掘 REST API 端点。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Query
from pydantic import BaseModel, Field

from src.api import deps
from src.api.schemas import ApiResponse
from src.utils.timezone import utc_now

logger = logging.getLogger(__name__)
router = APIRouter()


# ── 内存任务存储（与 backtest 的 runtime_store 同模式） ──────────

# §0cc P2：旧实现裸 dict 无容量上限 → 长期 API 进程内存随调用次数线性增长
# （每个完成结果常驻整份 result.to_dict()）。与 backtesting.data.store 50 条
# 上限对齐，FIFO 淘汰旧 completed/failed jobs；新写入必须走 _set_mining_job
# helper 强制走 cap 逻辑，禁止裸 dict 写入绕过淘汰。
_MINING_JOBS_MAX: int = 50
_mining_jobs: Dict[str, Dict[str, Any]] = {}


def _set_mining_job(run_id: str, payload: Dict[str, Any]) -> None:
    """§0cc P2：写入挂起结果时强制 FIFO 淘汰旧条目，避免内存无界增长。"""
    _mining_jobs[run_id] = payload
    # 新增的会移到 dict 末尾（py3.7+ 保序）；超容时从头部淘汰最旧
    while len(_mining_jobs) > _MINING_JOBS_MAX:
        oldest_key = next(iter(_mining_jobs))
        _mining_jobs.pop(oldest_key, None)


class MiningRunRequest(BaseModel):
    symbol: str
    timeframe: str
    start_time: str = Field(..., description="ISO start time")
    end_time: str = Field(..., description="ISO end time")
    analyses: Optional[List[str]] = Field(
        None, description="要运行的分析器列表（留空=全部）"
    )
    indicator_filter: Optional[List[str]] = Field(
        None, description="限定的指标列表（留空=全部）"
    )
    experiment_id: Optional[str] = None


def _get_research_repo():  # type: ignore[no-untyped-def]
    """获取共享的 ResearchRepository（来自 container.storage_writer）。

    禁止在此构造独立 TimescaleWriter——历史教训见 `deps.get_research_repo` 注释。
    """
    try:
        return deps.get_research_repo()
    except Exception:
        logger.debug("ResearchRepository not available", exc_info=True)
        return None


def _execute_mining(run_id: str, request: MiningRunRequest) -> None:
    """后台执行挖掘任务。

    §0cc P2：deps 用 with 自动 cleanup，避免 mining 入口连接池/线程池泄漏。
    任务状态写入全部走 _set_mining_job 强制 cap。
    """
    _set_mining_job(run_id, {
        "status": "running",
        "started_at": utc_now().isoformat(),
    })
    try:
        from src.backtesting.component_factory import build_research_data_deps
        from src.research.orchestration import MiningRunner

        with build_research_data_deps() as data_deps:
            runner = MiningRunner(deps=data_deps)
            result = runner.run(
                symbol=request.symbol,
                timeframe=request.timeframe,
                start_time=datetime.fromisoformat(request.start_time),
                end_time=datetime.fromisoformat(request.end_time),
                analyses=request.analyses,
                indicator_filter=request.indicator_filter,
            )
        result.experiment_id = request.experiment_id

        # 持久化
        repo = _get_research_repo()
        if repo is not None:
            repo.save_mining_result(result)

        # 更新 experiment：走 deps 取共享 repo 即可（ADR-006 + 避免每请求 new writer）
        if request.experiment_id:
            try:
                exp_repo = deps.get_experiment_repo()
                if exp_repo is not None:
                    exp_repo.link_to_mining_run(request.experiment_id, run_id)
            except Exception:
                logger.debug("Failed to update experiment", exc_info=True)

        _set_mining_job(run_id, {
            "status": "completed",
            "result": result.to_dict(),
        })
    except Exception as exc:
        logger.exception("Mining run %s failed", run_id)
        _set_mining_job(run_id, {"status": "failed", "error": str(exc)})


@router.post("/mining/run", response_model=ApiResponse)
def submit_mining_run(
    request: MiningRunRequest,
    background_tasks: BackgroundTasks,
) -> ApiResponse:
    """提交信号挖掘任务。"""
    from src.backtesting.models import generate_run_id

    run_id = f"mine_{generate_run_id()[3:]}"  # mine_xxxxxxxxxxxx
    _set_mining_job(run_id, {"status": "pending"})
    background_tasks.add_task(_execute_mining, run_id, request)
    return ApiResponse(
        success=True,
        data={"run_id": run_id, "status": "pending"},
    )


@router.get("/mining/{run_id}", response_model=ApiResponse)
def get_mining_result(run_id: str) -> ApiResponse:
    """查询挖掘结果（先查内存缓存，再查 DB）。"""
    # 内存缓存
    job = _mining_jobs.get(run_id)
    if job is not None:
        if job["status"] == "completed":
            return ApiResponse(success=True, data=job.get("result"))
        if job["status"] == "failed":
            return ApiResponse(success=False, error=job.get("error", "Unknown error"))
        return ApiResponse(
            success=True,
            data={"run_id": run_id, "status": job["status"]},
        )

    # DB 查询
    repo = _get_research_repo()
    if repo is not None:
        result = repo.fetch_mining_result(run_id)
        if result is not None:
            return ApiResponse(success=True, data=result)

    return ApiResponse(success=False, error=f"Mining run {run_id} not found")


@router.get("/mining", response_model=ApiResponse)
def list_mining_runs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> ApiResponse:
    """查询挖掘运行列表。"""
    repo = _get_research_repo()
    if repo is not None:
        runs = repo.list_mining_runs(limit=limit, offset=offset)
        return ApiResponse(success=True, data=runs)
    return ApiResponse(success=True, data=[])
