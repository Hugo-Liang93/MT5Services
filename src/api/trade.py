from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.api.deps import get_trading_service
from src.api.schemas import (
    ApiResponse,
    CancelOrdersRequest,
    CloseAllRequest,
    CloseRequest,
    EstimateMarginRequest,
    ModifyOrdersRequest,
    ModifyPositionsRequest,
    TradeRequest,
)
from src.core.trading_service import TradingService

router = APIRouter(tags=["trade"])


@router.post("/trade/open", response_model=ApiResponse[dict])
def trade_open(req: TradeRequest, svc: TradingService = Depends(get_trading_service)) -> ApiResponse[dict]:
    try:
        ticket = svc.open(
            symbol=req.symbol,
            volume=req.volume,
            side=req.side,
            price=req.price,
            sl=req.sl,
            tp=req.tp,
            deviation=req.deviation,
            comment=req.comment,
            magic=req.magic,
        )
        return ApiResponse(success=True, data={"ticket": ticket})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/trade/close", response_model=ApiResponse[dict])
def trade_close(req: CloseRequest, svc: TradingService = Depends(get_trading_service)) -> ApiResponse[dict]:
    try:
        ok = svc.close(ticket=req.ticket, deviation=req.deviation, comment=req.comment)
        return ApiResponse(success=True, data={"success": ok})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/trade/close-all", response_model=ApiResponse[dict])
def trade_close_all(req: CloseAllRequest, svc: TradingService = Depends(get_trading_service)) -> ApiResponse[dict]:
    """
    一键平仓（可按品种/魔术号/方向筛选）。
    """
    try:
        result = svc.close_all(
            symbol=req.symbol,
            magic=req.magic,
            side=req.side,
            deviation=req.deviation,
            comment=req.comment,
        )
        return ApiResponse(success=True, data=result)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/trade/cancel-orders", response_model=ApiResponse[dict])
def trade_cancel_orders(req: CancelOrdersRequest, svc: TradingService = Depends(get_trading_service)) -> ApiResponse[dict]:
    """
    批量撤销挂单（可按品种/魔术号过滤）。
    """
    try:
        result = svc.cancel_orders(symbol=req.symbol, magic=req.magic)
        return ApiResponse(success=True, data=result)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/trade/estimate-margin", response_model=ApiResponse[dict])
def trade_estimate_margin(req: EstimateMarginRequest, svc: TradingService = Depends(get_trading_service)) -> ApiResponse[dict]:
    """
    预估开仓所需保证金。
    """
    try:
        margin = svc.estimate_margin(symbol=req.symbol, volume=req.volume, side=req.side, price=req.price)
        return ApiResponse(success=True, data={"margin": margin})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/trade/modify-orders", response_model=ApiResponse[dict])
def trade_modify_orders(req: ModifyOrdersRequest, svc: TradingService = Depends(get_trading_service)) -> ApiResponse[dict]:
    """
    批量修改挂单止盈止损。
    """
    try:
        result = svc.modify_orders(symbol=req.symbol, magic=req.magic, sl=req.sl, tp=req.tp)
        return ApiResponse(success=True, data=result)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/trade/modify-positions", response_model=ApiResponse[dict])
def trade_modify_positions(req: ModifyPositionsRequest, svc: TradingService = Depends(get_trading_service)) -> ApiResponse[dict]:
    """
    批量修改持仓止盈止损。
    """
    try:
        result = svc.modify_positions(symbol=req.symbol, magic=req.magic, sl=req.sl, tp=req.tp)
        return ApiResponse(success=True, data=result)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
