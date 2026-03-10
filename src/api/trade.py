from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from src.api.deps import get_trading_service
from src.api.schemas import (
    ApiResponse,
    TradeRequest,
    CloseRequest,
    CloseAllRequest,
    CancelOrdersRequest,
    EstimateMarginRequest,
    ModifyOrdersRequest,
    ModifyPositionsRequest,
    PositionModel,
    OrderModel,
)
from src.api.error_codes import AIErrorCode, AIErrorAction, get_trade_error_details
from src.clients.mt5_trade import MT5TradeError
from src.core.trading_service import TradingService

router = APIRouter(tags=["trade"])


@router.post("/trade", response_model=ApiResponse[dict])
def trade(
    request: TradeRequest,
    service: TradingService = Depends(get_trading_service),
) -> ApiResponse[dict]:
    try:
        result = service.execute_trade(
            symbol=request.symbol,
            volume=request.volume,
            side=request.side,
            price=request.price,
            sl=request.sl,
            tp=request.tp,
            deviation=request.deviation,
            comment=request.comment,
            magic=request.magic,
        )
        
        if result is None:
            return ApiResponse.error_response(
                error_code=AIErrorCode.TRADE_EXECUTION_FAILED,
                error_message="浜ゆ槗鎵ц澶辫触",
                suggested_action=AIErrorAction.RETRY_AFTER_DELAY,
                details=get_trade_error_details(
                    symbol=request.symbol,
                    volume=request.volume,
                    side=request.side,
                    price=request.price
                )
            )
        
        return ApiResponse.success_response(
            data=result,
            metadata={
                "operation": "trade_execution",
                "symbol": request.symbol,
                "side": request.side,
                "volume": request.volume,
                "price": result.get("price") if result else None,
                "ticket": result.get("ticket") if result else None
            }
        )
    except MT5TradeError as exc:
        error_msg = str(exc)
        
        # 鏍规嵁閿欒娑堟伅鍒ゆ柇閿欒绫诲瀷
        if "insufficient margin" in error_msg.lower():
            error_code = AIErrorCode.INSUFFICIENT_MARGIN
            suggested_action = AIErrorAction.REDUCE_VOLUME
        elif "invalid volume" in error_msg.lower():
            error_code = AIErrorCode.INVALID_VOLUME
            suggested_action = AIErrorAction.ADJUST_PRICE
        elif "market closed" in error_msg.lower():
            error_code = AIErrorCode.MARKET_CLOSED
            suggested_action = AIErrorAction.WAIT_FOR_MARKET_OPEN
        elif "price" in error_msg.lower():
            error_code = AIErrorCode.INVALID_PRICE
            suggested_action = AIErrorAction.USE_MARKET_ORDER
        else:
            error_code = AIErrorCode.TRADE_EXECUTION_FAILED
            suggested_action = AIErrorAction.RETRY_AFTER_DELAY
        
        return ApiResponse.error_response(
            error_code=error_code,
            error_message=f"浜ゆ槗閿欒: {error_msg}",
            suggested_action=suggested_action,
            details={
                "exception_type": type(exc).__name__,
                **get_trade_error_details(
                    symbol=request.symbol,
                    volume=request.volume,
                    side=request.side,
                    price=request.price
                )
            }
        )


@router.post("/close", response_model=ApiResponse[dict])
def close(
    request: CloseRequest,
    service: TradingService = Depends(get_trading_service),
) -> ApiResponse[dict]:
    try:
        result = service.close_position(
            ticket=request.ticket,
            deviation=request.deviation,
            comment=request.comment,
        )
        
        if result is None:
            return ApiResponse.error_response(
                error_code=AIErrorCode.TRADE_CLOSE_FAILED,
                error_message="骞充粨澶辫触",
                suggested_action=AIErrorAction.CLOSE_POSITION,
                details={"ticket": request.ticket}
            )
        
        return ApiResponse.success_response(
            data=result,
            metadata={
                "operation": "position_close",
                "ticket": request.ticket,
                "success": result.get("success") if result else None,
                
            }
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.TRADE_CLOSE_FAILED,
            error_message=f"骞充粨閿欒: {str(exc)}",
            suggested_action=AIErrorAction.CLOSE_POSITION,
            details={
                "exception_type": type(exc).__name__,
                "ticket": request.ticket
            }
        )


@router.post("/close_all", response_model=ApiResponse[dict])
def close_all(
    request: CloseAllRequest,
    service: TradingService = Depends(get_trading_service),
) -> ApiResponse[dict]:
    try:
        result = service.close_all_positions(
            symbol=request.symbol,
            magic=request.magic,
            side=request.side,
            deviation=request.deviation,
            comment=request.comment,
        )
        
        return ApiResponse.success_response(
            data=result,
            metadata={
                "operation": "close_all_positions",
                "symbol": request.symbol,
                "magic": request.magic,
                "side": request.side,
                "closed_count": len(result.get("closed", [])),
                "failed_count": len(result.get("failed", []))
            }
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.TRADE_CLOSE_FAILED,
            error_message=f"鎵归噺骞充粨閿欒: {str(exc)}",
            suggested_action=AIErrorAction.CLOSE_POSITION,
            details={
                "exception_type": type(exc).__name__,
                "symbol": request.symbol,
                "magic": request.magic
            }
        )


@router.post("/cancel_orders", response_model=ApiResponse[dict])
def cancel_orders(
    request: CancelOrdersRequest,
    service: TradingService = Depends(get_trading_service),
) -> ApiResponse[dict]:
    try:
        result = service.cancel_orders(
            symbol=request.symbol,
            magic=request.magic,
        )
        
        return ApiResponse.success_response(
            data=result,
            metadata={
                "operation": "cancel_orders",
                "symbol": request.symbol,
                "magic": request.magic,
                "cancelled_count": len(result.get("cancelled", [])),
                "failed_count": len(result.get("failed", []))
            }
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.TRADE_CANCEL_FAILED,
            error_message=f"鍙栨秷璁㈠崟閿欒: {str(exc)}",
            suggested_action=AIErrorAction.CANCEL_ORDER,
            details={
                "exception_type": type(exc).__name__,
                "symbol": request.symbol,
                "magic": request.magic
            }
        )


@router.post("/estimate_margin", response_model=ApiResponse[dict])
def estimate_margin(
    request: EstimateMarginRequest,
    service: TradingService = Depends(get_trading_service),
) -> ApiResponse[dict]:
    try:
        margin = service.estimate_margin(
            symbol=request.symbol,
            volume=request.volume,
            side=request.side,
            price=request.price,
        )
        
        return ApiResponse.success_response(
            data={"margin": margin},
            metadata={
                "operation": "estimate_margin",
                "symbol": request.symbol,
                "volume": request.volume,
                "side": request.side,
                "price": request.price
            }
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.INSUFFICIENT_MARGIN,
            error_message=f"淇濊瘉閲戜及绠楅敊璇? {str(exc)}",
            suggested_action=AIErrorAction.REDUCE_VOLUME,
            details={
                "exception_type": type(exc).__name__,
                **get_trade_error_details(
                    symbol=request.symbol,
                    volume=request.volume,
                    side=request.side,
                    price=request.price
                )
            }
        )


@router.put("/modify_orders", response_model=ApiResponse[dict])
def modify_orders(
    request: ModifyOrdersRequest,
    service: TradingService = Depends(get_trading_service),
) -> ApiResponse[dict]:
    try:
        result = service.modify_orders(
            symbol=request.symbol,
            magic=request.magic,
            sl=request.sl,
            tp=request.tp,
        )
        
        return ApiResponse.success_response(
            data=result,
            metadata={
                "operation": "modify_orders",
                "symbol": request.symbol,
                "magic": request.magic,
                "modified_count": len(result.get("modified", [])),
                "failed_count": len(result.get("failed", []))
            }
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.TRADE_MODIFICATION_FAILED,
            error_message=f"淇敼璁㈠崟閿欒: {str(exc)}",
            suggested_action=AIErrorAction.MODIFY_POSITION,
            details={
                "exception_type": type(exc).__name__,
                "symbol": request.symbol,
                "magic": request.magic
            }
        )


@router.put("/modify_positions", response_model=ApiResponse[dict])
def modify_positions(
    request: ModifyPositionsRequest,
    service: TradingService = Depends(get_trading_service),
) -> ApiResponse[dict]:
    try:
        result = service.modify_positions(
            symbol=request.symbol,
            magic=request.magic,
            sl=request.sl,
            tp=request.tp,
        )
        
        return ApiResponse.success_response(
            data=result,
            metadata={
                "operation": "modify_positions",
                "symbol": request.symbol,
                "magic": request.magic,
                "modified_count": len(result.get("modified", [])),
                "failed_count": len(result.get("failed", []))
            }
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.TRADE_MODIFICATION_FAILED,
            error_message=f"淇敼鎸佷粨閿欒: {str(exc)}",
            suggested_action=AIErrorAction.MODIFY_POSITION,
            details={
                "exception_type": type(exc).__name__,
                "symbol": request.symbol,
                "magic": request.magic
            }
        )


@router.get("/positions", response_model=ApiResponse[List[PositionModel]])
def positions(
    symbol: Optional[str] = Query(default=None, description="trading symbol"),
    magic: Optional[int] = Query(default=None, description="magic id"),
    service: TradingService = Depends(get_trading_service),
) -> ApiResponse[List[PositionModel]]:
    try:
        positions = service.get_positions(symbol, magic)
        items = [PositionModel(**p.__dict__, time=p.time.isoformat()) for p in positions]
        
        return ApiResponse.success_response(
            data=items,
            metadata={
                "operation": "get_positions",
                "symbol": symbol,
                "magic": magic,
                "count": len(items),
                "total_volume": sum(p.volume for p in positions),
                "total_profit": sum(p.profit for p in positions) if positions and hasattr(positions[0], 'profit') else None
            }
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.POSITION_NOT_FOUND,
            error_message=f"鑾峰彇鎸佷粨閿欒: {str(exc)}",
            suggested_action=AIErrorAction.CHECK_ACCOUNT_STATUS,
            details={
                "exception_type": type(exc).__name__,
                "symbol": symbol,
                "magic": magic
            }
        )


@router.get("/orders", response_model=ApiResponse[List[OrderModel]])
def orders(
    symbol: Optional[str] = Query(default=None, description="trading symbol"),
    magic: Optional[int] = Query(default=None, description="magic id"),
    service: TradingService = Depends(get_trading_service),
) -> ApiResponse[List[OrderModel]]:
    try:
        orders = service.get_orders(symbol, magic)
        items = [OrderModel(**o.__dict__, time=o.time.isoformat()) for o in orders]
        
        return ApiResponse.success_response(
            data=items,
            metadata={
                "operation": "get_orders",
                "symbol": symbol,
                "magic": magic,
                "count": len(items),
                "total_volume": sum(o.volume for o in orders)
            }
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.ORDER_NOT_FOUND,
            error_message=f"鑾峰彇璁㈠崟閿欒: {str(exc)}",
            suggested_action=AIErrorAction.CHECK_ACCOUNT_STATUS,
            details={
                "exception_type": type(exc).__name__,
                "symbol": symbol,
                "magic": magic
            }
        )
