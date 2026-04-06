"""Paper Trading REST API 端点。"""

from __future__ import annotations

import configparser
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from src.api import deps
from src.api.schemas import ApiResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/status", response_model=ApiResponse)
def paper_trading_status() -> ApiResponse:
    """获取 Paper Trading 当前状态。"""
    bridge = deps.get_paper_trading_bridge()
    if bridge is None:
        return ApiResponse(
            success=True,
            data={"enabled": False, "running": False},
        )
    return ApiResponse(success=True, data=bridge.status())


@router.post("/start", response_model=ApiResponse)
def paper_trading_start() -> ApiResponse:
    """手动启动 Paper Trading session。"""
    bridge = deps.get_paper_trading_bridge()
    if bridge is None:
        return ApiResponse(
            success=False,
            error="Paper trading not configured (enabled=false in INI)",
        )
    if bridge.is_running():
        return ApiResponse(
            success=False,
            error="Paper trading session already running",
        )
    bridge.start()
    return ApiResponse(success=True, data=bridge.status())


@router.post("/start-from-recommendation", response_model=ApiResponse)
def paper_trading_start_from_recommendation(
    rec_id: str = Query(..., description="已 apply 的推荐记录 ID"),
) -> ApiResponse:
    """从已应用的 Recommendation 关联启动 Paper Trading。

    校验推荐状态为 APPLIED，然后启动 Paper Trading session 并携带
    experiment_id 和 source_backtest_run_id。
    """
    from src.api.backtest_routes.execution import get_backtest_repo

    bridge = deps.get_paper_trading_bridge()
    if bridge is None:
        return ApiResponse(
            success=False,
            error="Paper trading not configured (enabled=false in INI)",
        )
    if bridge.is_running():
        return ApiResponse(
            success=False,
            error="Paper trading session already running",
        )

    # 加载 Recommendation
    bt_repo = get_backtest_repo()
    if bt_repo is None:
        return ApiResponse(success=False, error="Backtest repository not available")

    rec = bt_repo.fetch_recommendation(rec_id)
    if rec is None:
        return ApiResponse(success=False, error=f"Recommendation {rec_id} not found")

    if rec.status.value != "applied":
        return ApiResponse(
            success=False,
            error=f"Recommendation status is '{rec.status.value}', expected 'applied'",
        )

    # 设置实验上下文并启动
    bridge.set_experiment_context(
        experiment_id=rec.experiment_id,
        source_backtest_run_id=rec.source_run_id,
    )
    bridge.start()

    return ApiResponse(
        success=True,
        data={
            **bridge.status(),
            "linked_recommendation": rec_id,
            "linked_backtest_run": rec.source_run_id,
        },
    )


@router.post("/stop", response_model=ApiResponse)
def paper_trading_stop() -> ApiResponse:
    """手动停止 Paper Trading session。"""
    bridge = deps.get_paper_trading_bridge()
    if bridge is None:
        return ApiResponse(success=False, error="Paper trading not configured")
    if not bridge.is_running():
        return ApiResponse(success=False, error="Paper trading not running")
    bridge.stop()
    # 保存 session
    tracker = deps._container.paper_trade_tracker if deps._container else None  # type: ignore[union-attr]
    if tracker is not None and bridge._session is not None:
        tracker.save_session(bridge._session)
    return ApiResponse(success=True, data=bridge.status())


@router.post("/reset", response_model=ApiResponse)
def paper_trading_reset() -> ApiResponse:
    """重置 Paper Trading（清空所有数据，需先 stop）。"""
    bridge = deps.get_paper_trading_bridge()
    if bridge is None:
        return ApiResponse(success=False, error="Paper trading not configured")
    bridge.reset()
    return ApiResponse(success=True, data={"reset": True})


@router.get("/trades", response_model=ApiResponse)
def paper_trading_trades() -> ApiResponse:
    """获取已平仓交易列表。"""
    bridge = deps.get_paper_trading_bridge()
    if bridge is None:
        return ApiResponse(success=True, data=[])
    return ApiResponse(success=True, data=bridge.get_closed_trades())


@router.get("/positions", response_model=ApiResponse)
def paper_trading_positions() -> ApiResponse:
    """获取当前持仓列表。"""
    bridge = deps.get_paper_trading_bridge()
    if bridge is None:
        return ApiResponse(success=True, data=[])
    return ApiResponse(success=True, data=bridge.get_open_positions())


@router.get("/metrics", response_model=ApiResponse)
def paper_trading_metrics() -> ApiResponse:
    """获取绩效指标。"""
    bridge = deps.get_paper_trading_bridge()
    if bridge is None:
        return ApiResponse(success=True, data={})
    return ApiResponse(success=True, data=bridge.get_metrics())


@router.get("/session", response_model=ApiResponse)
def paper_trading_session() -> ApiResponse:
    """获取当前 session 信息。"""
    bridge = deps.get_paper_trading_bridge()
    if bridge is None:
        return ApiResponse(success=True, data=None)
    return ApiResponse(success=True, data=bridge.get_session())


@router.get("/history/sessions", response_model=ApiResponse)
def paper_trading_session_history(limit: int = 50, offset: int = 0) -> ApiResponse:
    """查询历史 session 列表（从 DB）。"""
    container = deps._container  # type: ignore[union-attr]
    if container is None or container.storage_writer is None:
        return ApiResponse(success=True, data=[])
    try:
        repo = container.storage_writer.db.paper_trading_repo
        sessions = repo.fetch_sessions(limit=limit, offset=offset)
        return ApiResponse(success=True, data=sessions)
    except Exception as exc:
        return ApiResponse(success=False, error=str(exc))


@router.get("/history/trades", response_model=ApiResponse)
def paper_trading_trade_history(
    session_id: Optional[str] = None,
    strategy: Optional[str] = None,
    symbol: Optional[str] = None,
    limit: int = 200,
) -> ApiResponse:
    """查询历史交易记录（从 DB）。"""
    container = deps._container  # type: ignore[union-attr]
    if container is None or container.storage_writer is None:
        return ApiResponse(success=True, data=[])
    try:
        repo = container.storage_writer.db.paper_trading_repo
        trades = repo.fetch_trades(
            session_id=session_id,
            strategy=strategy,
            symbol=symbol,
            limit=limit,
        )
        return ApiResponse(success=True, data=trades)
    except Exception as exc:
        return ApiResponse(success=False, error=str(exc))


@router.get("/validate", response_model=ApiResponse)
def paper_trading_validate(
    backtest_run_id: str = Query(..., description="回测 run_id"),
    session_id: Optional[str] = Query(
        None, description="Paper session_id（空=当前 session）"
    ),
) -> ApiResponse:
    """Paper Trading vs Backtest 对比验证。

    对比当前（或指定）Paper Trading session 的绩效指标与回测结果，
    返回 ValidationVerdict（各指标偏差 + 是否通过）。
    """
    from src.backtesting.paper_trading.validation import compare_paper_vs_backtest

    # 获取 Paper Trading metrics
    bridge = deps.get_paper_trading_bridge()
    if bridge is None:
        return ApiResponse(success=False, error="Paper trading not configured")

    paper_metrics: Dict[str, Any]
    paper_sid: str
    experiment_id: Optional[str] = None

    if session_id is not None:
        # 从 DB 查询历史 session
        container = deps._container  # type: ignore[union-attr]
        if container is None or container.storage_writer is None:
            return ApiResponse(success=False, error="Storage not available")
        repo = container.storage_writer.db.paper_trading_repo
        summary = repo.fetch_performance_summary(session_id=session_id)
        if not summary:
            return ApiResponse(success=False, error=f"Session {session_id} not found")
        paper_metrics = summary
        paper_sid = session_id
    else:
        # 使用当前运行中的 session
        if not bridge.is_running():
            return ApiResponse(success=False, error="No active paper trading session")
        paper_metrics = bridge.get_metrics()
        session_info = bridge.get_session()
        paper_sid = session_info.get("session_id", "") if session_info else ""
        experiment_id = session_info.get("experiment_id") if session_info else None

    # 获取回测 metrics
    from src.api.backtest_routes.execution import get_backtest_repo

    bt_repo = get_backtest_repo()
    if bt_repo is None:
        return ApiResponse(success=False, error="Backtest repository not available")

    bt_result = bt_repo.fetch_run(backtest_run_id)
    if bt_result is None:
        return ApiResponse(
            success=False,
            error=f"Backtest run {backtest_run_id} not found",
        )

    backtest_metrics = bt_result.get("metrics", {})

    # 加载验证容差配置
    cfg = configparser.ConfigParser()
    cfg.read("config/paper_trading.ini", encoding="utf-8")
    min_trades = cfg.getint("validation", "min_trades_for_validation", fallback=20)
    wr_tol = cfg.getfloat("validation", "win_rate_tolerance_pct", fallback=15.0)
    sharpe_tol = cfg.getfloat("validation", "sharpe_tolerance_pct", fallback=50.0)
    dd_tol = cfg.getfloat("validation", "max_drawdown_tolerance_pct", fallback=50.0)

    verdict = compare_paper_vs_backtest(
        paper_metrics=paper_metrics,
        backtest_metrics=backtest_metrics,
        min_paper_trades=min_trades,
        win_rate_tolerance_pct=wr_tol,
        sharpe_tolerance_pct=sharpe_tol,
        max_drawdown_tolerance_pct=dd_tol,
        paper_session_id=paper_sid,
        backtest_run_id=backtest_run_id,
        experiment_id=experiment_id,
    )

    return ApiResponse(success=True, data=verdict.to_dict())


@router.get("/history/performance", response_model=ApiResponse)
def paper_trading_performance_summary(
    session_id: Optional[str] = None,
) -> ApiResponse:
    """查询绩效汇总（按策略分组，从 DB）。"""
    container = deps._container  # type: ignore[union-attr]
    if container is None or container.storage_writer is None:
        return ApiResponse(success=True, data={})
    try:
        repo = container.storage_writer.db.paper_trading_repo
        summary = repo.fetch_performance_summary(session_id=session_id)
        return ApiResponse(success=True, data=summary)
    except Exception as exc:
        return ApiResponse(success=False, error=str(exc))
