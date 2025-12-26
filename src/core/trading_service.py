"""
交易业务层：封装下单/平仓，校验参数。
"""

from __future__ import annotations

from typing import Optional

from src.clients.mt5_trading import MT5TradingClient, MT5TradingClientError


class TradingService:
    def __init__(self, client: Optional[MT5TradingClient] = None):
        self.client = client or MT5TradingClient()

    def open(
        self,
        symbol: str,
        volume: float,
        side: str,
        price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        deviation: int = 20,
        comment: str = "",
        magic: int = 0,
    ) -> int:
        order_type = self._side_to_order_type(side)
        return self.client.open_trade(
            symbol=symbol,
            volume=volume,
            order_type=order_type,
            price=price,
            sl=sl,
            tp=tp,
            deviation=deviation,
            comment=comment,
            magic=magic,
        )

    def close(self, ticket: int, deviation: int = 20, comment: str = "") -> bool:
        return self.client.close_position(ticket, deviation=deviation, comment=comment)

    def close_all(
        self,
        symbol: Optional[str] = None,
        magic: Optional[int] = None,
        side: Optional[str] = None,
        deviation: int = 20,
        comment: str = "close_all",
    ) -> dict:
        return self.client.close_positions(
            symbol=symbol,
            magic=magic,
            side=side,
            deviation=deviation,
            comment=comment,
        )

    def cancel_orders(self, symbol: Optional[str] = None, magic: Optional[int] = None) -> dict:
        return self.client.cancel_orders(symbol=symbol, magic=magic)

    def estimate_margin(self, symbol: str, volume: float, side: str, price: Optional[float] = None) -> float:
        return self.client.estimate_margin(symbol=symbol, volume=volume, side=side, price=price)

    def modify_orders(
        self,
        symbol: Optional[str] = None,
        magic: Optional[int] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> dict:
        return self.client.modify_orders(symbol=symbol, magic=magic, sl=sl, tp=tp)

    def modify_positions(
        self,
        symbol: Optional[str] = None,
        magic: Optional[int] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> dict:
        return self.client.modify_positions(symbol=symbol, magic=magic, sl=sl, tp=tp)

    def _side_to_order_type(self, side: str) -> int:
        if not hasattr(self.client, "connect"):
            raise MT5TradingClientError("Trading client not initialized")
        side_lower = side.lower()
        if side_lower in ("buy", "long"):
            return __import__("MetaTrader5").ORDER_TYPE_BUY
        if side_lower in ("sell", "short"):
            return __import__("MetaTrader5").ORDER_TYPE_SELL
        raise MT5TradingClientError(f"Unsupported side: {side}")
