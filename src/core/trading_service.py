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
        order_kind: str = "market",
        price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        deviation: int = 20,
        comment: str = "",
        magic: int = 0,
    ) -> int:
        order_type = self._side_to_order_type(side, order_kind=order_kind)
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

    def close(
        self,
        ticket: int,
        deviation: int = 20,
        comment: str = "",
        volume: Optional[float] = None,
    ) -> bool:
        return self.client.close_position(ticket, deviation=deviation, comment=comment, volume=volume)

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

    def _side_to_order_type(self, side: str, order_kind: str = "market") -> int:
        if hasattr(self.client, "side_and_kind_to_order_type"):
            return self.client.side_and_kind_to_order_type(side, order_kind)
        if not hasattr(self.client, "connect"):
            raise MT5TradingClientError("Trading client not initialized")
        side_lower = side.lower()
        if order_kind.lower() != "market":
            raise MT5TradingClientError(f"Unsupported order kind without native client support: {order_kind}")
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
        order_kind: str = "market",
        price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        deviation: int = 20,
        comment: str = "",
        magic: int = 0,
    ) -> dict:
        risk_assessment: Optional[Dict[str, Any]] = None
        if self.pre_trade_risk_service is not None:
            risk_assessment = self.pre_trade_risk_service.enforce_trade_allowed(
                symbol=symbol,
                volume=volume,
                side=side,
                order_kind=order_kind,
                price=price,
                sl=sl,
                tp=tp,
                deviation=deviation,
                comment=comment,
                magic=magic,
            )
        margin_estimate: Optional[float] = None
        try:
            margin_estimate = self.estimate_margin(symbol=symbol, volume=volume, side=side, price=price)
        except Exception:
            margin_estimate = None

        if hasattr(self.client, "open_trade_details"):
            result = self.client.open_trade_details(
                symbol=symbol,
                volume=volume,
                order_type=self._side_to_order_type(side, order_kind=order_kind),
                price=price,
                sl=sl,
                tp=tp,
                deviation=deviation,
                comment=comment,
                magic=magic,
            )
        else:
            ticket = self.open(
                symbol=symbol,
                volume=volume,
                side=side,
                order_kind=order_kind,
                price=price,
                sl=sl,
                tp=tp,
                deviation=deviation,
                comment=comment,
                magic=magic,
            )
            result = {"ticket": ticket, "price": price}

        result.update(
            {
                "symbol": symbol,
                "volume": volume,
                "side": side,
                "order_kind": order_kind,
                "requested_price": price,
                "estimated_margin": margin_estimate,
                "pre_trade_risk": risk_assessment,
            }
        )
        return result

    def precheck_trade(
        self,
        symbol: str,
        volume: Optional[float] = None,
        side: Optional[str] = None,
        order_kind: str = "market",
        price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        deviation: int = 20,
        comment: str = "",
        magic: int = 0,
    ) -> Dict[str, Any]:
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
                "checks": [],
            }
        assessment = self.pre_trade_risk_service.assess_trade(
            symbol=symbol,
            volume=volume,
            side=side,
            order_kind=order_kind,
            price=price,
            sl=sl,
            tp=tp,
            deviation=deviation,
            comment=comment,
            magic=magic,
        )
        assessment["estimated_margin"] = None
        if volume is not None and side:
            try:
                assessment["estimated_margin"] = self.estimate_margin(
                    symbol=symbol,
                    volume=volume,
                    side=side,
                    price=price,
                )
            except Exception as exc:
                assessment.setdefault("warnings", []).append(f"Margin estimate unavailable: {exc}")
                assessment["margin_error"] = str(exc)
        return assessment

    def close_position(
        self,
        ticket: int,
        deviation: int = 20,
        comment: str = "",
        volume: Optional[float] = None,
    ) -> dict:
        success = self.close(ticket=ticket, deviation=deviation, comment=comment, volume=volume)
        return {"ticket": ticket, "success": success, "volume": volume}

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

    def execute_trade_batch(self, trades: list[dict], stop_on_error: bool = False) -> dict:
        results = []
        success_count = 0
        failure_count = 0
        for index, trade in enumerate(trades):
            try:
                result = self.execute_trade(**trade)
                results.append({"index": index, "success": True, "result": result})
                success_count += 1
            except Exception as exc:
                results.append({"index": index, "success": False, "error": str(exc), "trade": dict(trade)})
                failure_count += 1
                if stop_on_error:
                    break
        return {
            "results": results,
            "success_count": success_count,
            "failure_count": failure_count,
            "stop_on_error": stop_on_error,
        }

    def close_positions_by_tickets(
        self,
        tickets: list[int],
        deviation: int = 20,
        comment: str = "close_batch",
    ) -> dict:
        if hasattr(self.client, "close_positions_by_tickets"):
            return self.client.close_positions_by_tickets(
                tickets=tickets,
                deviation=deviation,
                comment=comment,
            )
        closed, failed = [], []
        for ticket in tickets:
            try:
                self.close(ticket=ticket, deviation=deviation, comment=comment)
                closed.append(ticket)
            except Exception as exc:
                failed.append({"ticket": ticket, "error": str(exc)})
        return {"closed": closed, "failed": failed}

    def cancel_orders_by_tickets(self, tickets: list[int]) -> dict:
        if hasattr(self.client, "cancel_orders_by_tickets"):
            return self.client.cancel_orders_by_tickets(tickets)
        return {"canceled": [], "failed": [{"ticket": int(ticket), "error": "unsupported"} for ticket in tickets]}
