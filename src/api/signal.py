from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.deps import get_position_manager, get_signal_runtime, get_signal_service, get_trading_service
from src.signals.position_manager import PositionManager
from src.api.schemas import (
    ApiResponse,
    SignalDecisionModel,
    SignalEvaluateRequest,
    SignalEventModel,
    SignalExecuteTradeRequest,
    SignalSummaryModel,
)
from src.signals.runtime import SignalRuntime
from src.signals.service import SignalModule
from src.signals.sizing import compute_trade_params, extract_atr_from_indicators
from src.trading.service import TradingModule

router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("/strategies", response_model=ApiResponse[list[str]])
def list_signal_strategies(
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[list[str]]:
    strategies = service.list_strategies()
    return ApiResponse.success_response(
        data=strategies,
        metadata={"count": len(strategies)},
    )


@router.post("/evaluate", response_model=ApiResponse[SignalDecisionModel])
def evaluate_signal(
    request: SignalEvaluateRequest,
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[SignalDecisionModel]:
    decision = service.evaluate(
        symbol=request.symbol,
        timeframe=request.timeframe,
        strategy=request.strategy,
        indicators=request.indicators or None,
        metadata=request.metadata,
    )
    return ApiResponse.success_response(
        data=SignalDecisionModel(**decision.to_dict()),
        metadata={
            "symbol": request.symbol,
            "timeframe": request.timeframe,
            "strategy": request.strategy,
            "persisted": True,
        },
    )


@router.get("/recent", response_model=ApiResponse[list[SignalEventModel]])
def recent_signals(
    symbol: Optional[str] = Query(default=None),
    timeframe: Optional[str] = Query(default=None),
    strategy: Optional[str] = Query(default=None),
    action: Optional[str] = Query(default=None),
    scope: str = Query(default="confirmed", pattern="^(confirmed|preview|all)$"),
    limit: int = Query(default=200, ge=1, le=2000),
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[list[SignalEventModel]]:
    rows = service.recent_signals(
        symbol=symbol,
        timeframe=timeframe,
        strategy=strategy,
        action=action,
        scope=scope,
        limit=limit,
    )
    return ApiResponse.success_response(
        data=[SignalEventModel(**row) for row in rows],
        metadata={"count": len(rows), "scope": scope},
    )


@router.get("/summary", response_model=ApiResponse[list[SignalSummaryModel]])
def signal_summary(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    scope: str = Query(default="confirmed", pattern="^(confirmed|preview|all)$"),
    service: SignalModule = Depends(get_signal_service),
) -> ApiResponse[list[SignalSummaryModel]]:
    rows = service.summary(hours=hours, scope=scope)
    return ApiResponse.success_response(
        data=[SignalSummaryModel(**row) for row in rows],
        metadata={"hours": hours, "count": len(rows), "scope": scope},
    )


@router.get("/runtime/status", response_model=ApiResponse[dict])
def signal_runtime_status(service: SignalRuntime = Depends(get_signal_runtime)) -> ApiResponse[dict]:
    return ApiResponse.success_response(data=service.status())


@router.get("/positions", response_model=ApiResponse[list])
def get_tracked_positions(
    manager: PositionManager = Depends(get_position_manager),
) -> ApiResponse[list]:
    """Return positions currently tracked by the signal position manager."""
    positions = manager.active_positions()
    return ApiResponse.success_response(
        data=positions,
        metadata={"count": len(positions), **manager.status()},
    )


@router.post("/execute-trade", response_model=ApiResponse[Dict[str, Any]])
def execute_trade_from_signal(
    request: SignalExecuteTradeRequest,
    signal_service: SignalModule = Depends(get_signal_service),
    trading_service: TradingModule = Depends(get_trading_service),
) -> ApiResponse[Dict[str, Any]]:
    """AI agent dispatch path: look up a confirmed signal and execute the trade.

    The system computes ATR-based SL/TP from the stored indicator snapshot,
    then submits the order through TradingModule (full risk checks apply).

    Trigger modes:
    - Event-driven (auto): SignalRuntime publishes SignalEvent → TradeExecutor subscriber
    - API-dispatched (manual/AI agent): call this endpoint with a signal_id
    """
    rows = signal_service.recent_signals(scope="confirmed", limit=500)
    signal_row = next((r for r in rows if r.get("signal_id") == request.signal_id), None)
    if signal_row is None:
        raise HTTPException(status_code=404, detail=f"Signal not found: {request.signal_id}")

    action = signal_row.get("action", "")
    if action not in ("buy", "sell"):
        raise HTTPException(
            status_code=422,
            detail=f"Signal action '{action}' is not executable (buy/sell required)",
        )

    indicators: Dict[str, Any] = signal_row.get("indicators_snapshot") or {}
    atr = extract_atr_from_indicators(indicators)
    if atr is None or atr <= 0:
        raise HTTPException(status_code=422, detail="Cannot compute trade params: ATR not found in signal snapshot")

    try:
        balance = float(
            (trading_service.account_info() or {}).get("equity")
            or (trading_service.account_info() or {}).get("balance")
            or 0
        )
    except Exception:
        balance = 0.0
    if balance <= 0:
        raise HTTPException(status_code=422, detail="Cannot compute position size: account balance unavailable")

    # Estimate entry price from indicator snapshot
    entry_price: Optional[float] = None
    for ind_name in ("bollinger20", "sma20", "close", "price"):
        payload = indicators.get(ind_name)
        if isinstance(payload, dict):
            for fld in ("close", "value", "last", "bb_mid", "sma"):
                val = payload.get(fld)
                if val is not None:
                    try:
                        entry_price = float(val)
                        break
                    except (TypeError, ValueError):
                        continue
        if entry_price:
            break

    if entry_price is None or entry_price <= 0:
        raise HTTPException(status_code=422, detail="Cannot estimate entry price from signal snapshot")

    params = compute_trade_params(
        action=action,
        current_price=entry_price,
        atr_value=atr,
        account_balance=balance,
    )
    volume = request.volume_override if request.volume_override is not None else params.position_size

    trade_payload: Dict[str, Any] = {
        "symbol": signal_row.get("symbol"),
        "volume": volume,
        "side": action,
        "order_kind": "market",
        "sl": params.stop_loss,
        "tp": params.take_profit,
        "comment": f"agent:{signal_row.get('strategy')}:{action}:{request.signal_id[:8]}",
    }
    result = trading_service.dispatch_operation("trade", trade_payload)
    return ApiResponse.success_response(
        data=result if isinstance(result, dict) else {"result": result},
        metadata={
            "signal_id": request.signal_id,
            "action": action,
            "volume": volume,
            "sl": params.stop_loss,
            "tp": params.take_profit,
            "risk_reward_ratio": params.risk_reward_ratio,
        },
    )

