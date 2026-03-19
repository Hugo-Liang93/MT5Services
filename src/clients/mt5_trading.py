"""
MT5 交易封装：下单/平仓等写操作。
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

from src.clients.base import MT5BaseClient, mt5
from src.config import MT5Settings
from src.clients.mt5_account import Position
from src.clients.base import MT5TradeError


class MT5TradingClientError(MT5TradeError):
    pass


class MT5TradingClient(MT5BaseClient):
    def __init__(self, settings: Optional[MT5Settings] = None):
        super().__init__(settings=settings)

    @staticmethod
    def _pending_order_types() -> set[int]:
        return {
            getattr(mt5, "ORDER_TYPE_BUY_LIMIT", -1),
            getattr(mt5, "ORDER_TYPE_SELL_LIMIT", -1),
            getattr(mt5, "ORDER_TYPE_BUY_STOP", -1),
            getattr(mt5, "ORDER_TYPE_SELL_STOP", -1),
            getattr(mt5, "ORDER_TYPE_BUY_STOP_LIMIT", -1),
            getattr(mt5, "ORDER_TYPE_SELL_STOP_LIMIT", -1),
        }

    @staticmethod
    def _buy_order_types() -> set[int]:
        return {
            mt5.ORDER_TYPE_BUY,
            getattr(mt5, "ORDER_TYPE_BUY_LIMIT", mt5.ORDER_TYPE_BUY),
            getattr(mt5, "ORDER_TYPE_BUY_STOP", mt5.ORDER_TYPE_BUY),
            getattr(mt5, "ORDER_TYPE_BUY_STOP_LIMIT", mt5.ORDER_TYPE_BUY),
        }

    def _validate_protection_levels(
        self,
        *,
        order_type: int,
        request_price: float,
        sl: Optional[float],
        tp: Optional[float],
    ) -> None:
        is_buy = order_type in self._buy_order_types()
        if sl is not None:
            if is_buy and sl >= request_price:
                raise MT5TradingClientError("Stop loss must be below entry price for buy orders")
            if not is_buy and sl <= request_price:
                raise MT5TradingClientError("Stop loss must be above entry price for sell orders")
        if tp is not None:
            if is_buy and tp <= request_price:
                raise MT5TradingClientError("Take profit must be above entry price for buy orders")
            if not is_buy and tp >= request_price:
                raise MT5TradingClientError("Take profit must be below entry price for sell orders")

    def side_and_kind_to_order_type(self, side: str, order_kind: str = "market") -> int:
        side_lower = side.lower()
        order_kind_lower = order_kind.lower()
        if side_lower not in {"buy", "long", "sell", "short"}:
            raise MT5TradingClientError(f"Unsupported side: {side}")
        is_buy = side_lower in {"buy", "long"}
        if order_kind_lower == "market":
            return mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        if order_kind_lower == "limit":
            return getattr(mt5, "ORDER_TYPE_BUY_LIMIT", mt5.ORDER_TYPE_BUY) if is_buy else getattr(mt5, "ORDER_TYPE_SELL_LIMIT", mt5.ORDER_TYPE_SELL)
        if order_kind_lower == "stop":
            return getattr(mt5, "ORDER_TYPE_BUY_STOP", mt5.ORDER_TYPE_BUY) if is_buy else getattr(mt5, "ORDER_TYPE_SELL_STOP", mt5.ORDER_TYPE_SELL)
        if order_kind_lower == "stop_limit":
            return getattr(mt5, "ORDER_TYPE_BUY_STOP_LIMIT", mt5.ORDER_TYPE_BUY) if is_buy else getattr(mt5, "ORDER_TYPE_SELL_STOP_LIMIT", mt5.ORDER_TYPE_SELL)
        raise MT5TradingClientError(f"Unsupported order kind: {order_kind}")

    def open_trade_details(
        self,
        symbol: str,
        volume: float,
        order_type: int,
        price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        deviation: int = 20,
        comment: str = "",
        magic: int = 0,
    ) -> dict:
        self.connect()
        self._validate_volume(symbol, volume)
        is_pending = order_type in self._pending_order_types()
        if is_pending and price is None:
            raise MT5TradingClientError("Pending orders require an explicit price")
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise MT5TradingClientError(f"Failed to get tick for {symbol}: {mt5.last_error()}")
        request_price = price
        if request_price is None:
            request_price = tick.ask if order_type in self._buy_order_types() else tick.bid
        self._validate_protection_levels(
            order_type=order_type,
            request_price=request_price,
            sl=sl,
            tp=tp,
        )
        request = {
            "action": mt5.TRADE_ACTION_PENDING if is_pending else mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": request_price,
            "sl": sl or 0.0,
            "tp": tp or 0.0,
            "deviation": deviation,
            "magic": magic,
            "comment": comment,
            "type_filling": mt5.ORDER_FILLING_FOK,
        }
        result = mt5.order_send(request)
        success_codes = {mt5.TRADE_RETCODE_DONE}
        if is_pending:
            success_codes.add(getattr(mt5, "TRADE_RETCODE_PLACED", mt5.TRADE_RETCODE_DONE))
        if result is None or result.retcode not in success_codes:
            raise MT5TradingClientError(f"Order send failed: {result and result.comment}")
        ticket = int(getattr(result, "order", 0) or getattr(result, "deal", 0) or 0)
        return {
            "ticket": ticket,
            "order": int(getattr(result, "order", 0) or 0),
            "deal": int(getattr(result, "deal", 0) or 0),
            "retcode": int(result.retcode),
            "comment": getattr(result, "comment", ""),
            "symbol": symbol,
            "volume": volume,
            "price": request_price,
            "sl": sl,
            "tp": tp,
            "deviation": deviation,
            "magic": magic,
            "pending": is_pending,
        }

    def open_trade(
        self,
        symbol: str,
        volume: float,
        order_type: int,
        price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        deviation: int = 20,
        comment: str = "",
        magic: int = 0,
    ) -> int:
        details = self.open_trade_details(
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
        return int(details["ticket"])

    def cancel_orders(self, symbol: Optional[str] = None, magic: Optional[int] = None) -> dict:
        """
        批量撤销挂单，可按品种/魔术号过滤。
        """
        self.connect()
        orders = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
        if orders is None:
            raise MT5TradingClientError(f"Failed to get orders: {mt5.last_error()}")
        targets = [o for o in orders if (magic is None or o.magic == magic)]
        canceled, failed = [], []
        for o in targets:
            req = {
                "action": mt5.TRADE_ACTION_REMOVE,
                "order": o.ticket,
                "symbol": o.symbol,
                "comment": "cancel_all",
            }
            result = mt5.order_send(req)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                failed.append({"ticket": o.ticket, "symbol": o.symbol, "error": result and result.comment})
            else:
                canceled.append(o.ticket)
        return {"canceled": canceled, "failed": failed}

    def cancel_orders_by_tickets(self, tickets: list[int]) -> dict:
        self.connect()
        requested = {int(ticket) for ticket in tickets}
        if not requested:
            return {"canceled": [], "failed": []}
        orders = mt5.orders_get()
        if orders is None:
            raise MT5TradingClientError(f"Failed to get orders: {mt5.last_error()}")
        order_map = {int(order.ticket): order for order in orders if int(order.ticket) in requested}
        canceled, failed = [], []
        for ticket in requested:
            order = order_map.get(ticket)
            if order is None:
                failed.append({"ticket": ticket, "error": "order_not_found"})
                continue
            req = {
                "action": mt5.TRADE_ACTION_REMOVE,
                "order": order.ticket,
                "symbol": order.symbol,
                "comment": "cancel_batch",
            }
            result = mt5.order_send(req)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                failed.append({"ticket": order.ticket, "symbol": order.symbol, "error": result and result.comment})
            else:
                canceled.append(order.ticket)
        return {"canceled": canceled, "failed": failed}

    def estimate_margin(self, symbol: str, volume: float, side: str, price: Optional[float] = None) -> float:
        """
        预估开仓所需保证金，便于上层风控。
        """
        self.connect()
        self._validate_volume(symbol, volume)
        order_type = self._side_to_order_type(side)
        tick = mt5.symbol_info_tick(symbol)
        price = price or (tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid)
        margin = mt5.order_calc_margin(order_type, symbol, volume, price)
        if margin is None:
            raise MT5TradingClientError(f"Failed to calc margin: {mt5.last_error()}")
        return margin

    def close_position(
        self,
        ticket: int,
        deviation: int = 20,
        comment: str = "",
        volume: Optional[float] = None,
    ) -> bool:
        self.connect()
        position = mt5.positions_get(ticket=ticket)
        if not position:
            raise MT5TradingClientError(f"Position {ticket} not found")
        pos = position[0]
        close_volume = float(volume if volume is not None else pos.volume)
        if close_volume <= 0 or close_volume > pos.volume:
            raise MT5TradingClientError(f"Close volume {close_volume} invalid for position {ticket}")
        tick = mt5.symbol_info_tick(pos.symbol)
        price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": close_volume,
            "type": mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY,
            "position": pos.ticket,
            "price": price,
            "deviation": deviation,
            "magic": pos.magic,
            "comment": comment or "close",
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            raise MT5TradingClientError(f"Close failed: {result and result.comment}")
        return True

    def close_positions_by_tickets(
        self,
        tickets: list[int],
        deviation: int = 20,
        comment: str = "close_batch",
    ) -> dict:
        requested = [int(ticket) for ticket in tickets]
        closed, failed = [], []
        for ticket in requested:
            try:
                self.close_position(ticket=ticket, deviation=deviation, comment=comment)
                closed.append(ticket)
            except Exception as exc:
                failed.append({"ticket": ticket, "error": str(exc)})
        return {"closed": closed, "failed": failed}

    def close_positions(
        self,
        symbol: Optional[str] = None,
        magic: Optional[int] = None,
        side: Optional[str] = None,
        deviation: int = 20,
        comment: str = "close_all",
    ) -> dict:
        """
        一键平仓（可按品种/魔术号/方向筛选），返回成功/失败的 ticket 列表。
        """
        self.connect()
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if positions is None:
            raise MT5TradingClientError(f"Failed to get positions: {mt5.last_error()}")

        def _match_side(pos) -> bool:
            if side is None:
                return True
            side_lower = side.lower()
            return (side_lower in ("buy", "long") and pos.type == mt5.ORDER_TYPE_BUY) or (
                side_lower in ("sell", "short") and pos.type == mt5.ORDER_TYPE_SELL
            )

        def _match_magic(pos) -> bool:
            return True if magic is None else pos.magic == magic

        targets = [p for p in positions if _match_side(p) and _match_magic(p)]
        closed, failed = [], []
        for pos in targets:
            try:
                tick = mt5.symbol_info_tick(pos.symbol)
                price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": pos.symbol,
                    "volume": pos.volume,
                    "type": mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                    "position": pos.ticket,
                    "price": price,
                    "deviation": deviation,
                    "magic": pos.magic,
                    "comment": comment,
                }
                result = mt5.order_send(request)
                if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                    raise MT5TradingClientError(f"Close failed: {result and result.comment}")
                closed.append(pos.ticket)
            except Exception as exc:
                failed.append({"ticket": pos.ticket, "symbol": pos.symbol, "error": str(exc)})
        return {"closed": closed, "failed": failed}

    def _to_tz(self, dt: datetime) -> datetime:
        return dt.astimezone(self.tz)

    def modify_orders(
        self,
        symbol: Optional[str] = None,
        magic: Optional[int] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> dict:
        """
        批量修改挂单的止盈/止损。
        """
        self.connect()
        orders = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
        if orders is None:
            raise MT5TradingClientError(f"Failed to get orders: {mt5.last_error()}")
        targets = [o for o in orders if (magic is None or o.magic == magic)]
        modified, failed = [], []
        for o in targets:
            req = {
                "action": mt5.TRADE_ACTION_MODIFY,
                "order": o.ticket,
                "symbol": o.symbol,
                "sl": sl if sl is not None else o.sl,
                "tp": tp if tp is not None else o.tp,
            }
            result = mt5.order_send(req)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                failed.append({"ticket": o.ticket, "symbol": o.symbol, "error": result and result.comment})
            else:
                modified.append(o.ticket)
        return {"modified": modified, "failed": failed}

    def modify_positions(
        self,
        symbol: Optional[str] = None,
        magic: Optional[int] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> dict:
        """
        批量修改持仓的止盈/止损。
        """
        self.connect()
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if positions is None:
            raise MT5TradingClientError(f"Failed to get positions: {mt5.last_error()}")
        targets = [p for p in positions if (magic is None or p.magic == magic)]
        modified, failed = [], []
        for p in targets:
            req = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": p.ticket,
                "symbol": p.symbol,
                "sl": sl if sl is not None else p.sl,
                "tp": tp if tp is not None else p.tp,
            }
            result = mt5.order_send(req)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                failed.append({"ticket": p.ticket, "symbol": p.symbol, "error": result and result.comment})
            else:
                modified.append(p.ticket)
        return {"modified": modified, "failed": failed}

    def _validate_volume(self, symbol: str, volume: float) -> None:
        """
        按品种元数据校验下单手数：最小/最大/步长。
        """
        info = mt5.symbol_info(symbol)
        if info is None:
            raise MT5TradingClientError(f"Symbol {symbol} not found: {mt5.last_error()}")
        vol_min = info.volume_min or 0.0
        vol_max = info.volume_max or float("inf")
        step = info.volume_step or 0.0
        if volume < vol_min or volume > vol_max:
            raise MT5TradingClientError(f"Volume {volume} out of range [{vol_min}, {vol_max}] for {symbol}")
        if step > 0:
            steps = round(volume / step)
            if not math.isclose(steps * step, volume, rel_tol=1e-9, abs_tol=1e-9):
                raise MT5TradingClientError(f"Volume {volume} is not aligned to step {step} for {symbol}")
