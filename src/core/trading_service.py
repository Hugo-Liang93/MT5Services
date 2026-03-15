"""
交易业务层：封装下单/平仓，校验参数。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.clients.mt5_trading import MT5TradingClient, MT5TradingClientError
from src.clients.mt5_account import MT5AccountClient
from src.core.pretrade_risk_service import PreTradeRiskService


class TradingService:
    def __init__(
        self,
        client: Optional[MT5TradingClient] = None,
        account_client: Optional[MT5AccountClient] = None,
        pre_trade_risk_service: Optional[PreTradeRiskService] = None,
    ):
        self.client = client or MT5TradingClient()
        self.account_client = account_client or MT5AccountClient()
        self.pre_trade_risk_service = pre_trade_risk_service

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

    # --- Backward-compatible API expected by src.api.trade ---
    def execute_trade(
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
    ) -> dict:
        risk_assessment: Optional[Dict[str, Any]] = None
        if self.pre_trade_risk_service is not None:
            risk_assessment = self.pre_trade_risk_service.enforce_trade_allowed(symbol=symbol)
        ticket = self.open(
            symbol=symbol,
            volume=volume,
            side=side,
            price=price,
            sl=sl,
            tp=tp,
            deviation=deviation,
            comment=comment,
            magic=magic,
        )
        return {
            "ticket": ticket,
            "symbol": symbol,
            "volume": volume,
            "side": side,
            "price": price,
            "pre_trade_risk": risk_assessment,
        }

    def precheck_trade(self, symbol: str) -> Dict[str, Any]:
        if self.pre_trade_risk_service is None:
            return {
                "enabled": False,
                "mode": "off",
                "blocked": False,
                "action": "allow",
                "reason": None,
                "symbol": symbol,
                "active_windows": [],
                "upcoming_windows": [],
            }
        return self.pre_trade_risk_service.assess_trade(symbol=symbol)

    def close_position(self, ticket: int, deviation: int = 20, comment: str = "") -> dict:
        success = self.close(ticket=ticket, deviation=deviation, comment=comment)
        return {"ticket": ticket, "success": success}

    def close_all_positions(
        self,
        symbol: Optional[str] = None,
        magic: Optional[int] = None,
        side: Optional[str] = None,
        deviation: int = 20,
        comment: str = "close_all",
    ) -> dict:
        return self.close_all(
            symbol=symbol,
            magic=magic,
            side=side,
            deviation=deviation,
            comment=comment,
        )

    def get_positions(self, symbol: Optional[str] = None, magic: Optional[int] = None):
        positions = self.account_client.positions(symbol=symbol)
        if magic is not None:
            positions = [p for p in positions if p.magic == magic]
        return positions

    def get_orders(self, symbol: Optional[str] = None, magic: Optional[int] = None):
        orders = self.account_client.orders(symbol=symbol)
        if magic is not None:
            orders = [o for o in orders if o.magic == magic]
        return orders
