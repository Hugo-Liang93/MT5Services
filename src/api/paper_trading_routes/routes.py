"""Paper Trading REST API 端点。"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends

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
