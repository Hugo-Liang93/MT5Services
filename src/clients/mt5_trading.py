"""
MT5 交易封装：下单/平仓等写操作。
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

from src.clients.base import MT5BaseClient, MT5BaseError, mt5
from src.clients.mt5_account import Position


class MT5TradingClientError(MT5BaseError):
    pass


class MT5TradingClient(MT5BaseClient):
    def __init__(self):
        super().__init__()

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
        """
        下单：order_type 可用 mt5.ORDER_TYPE_BUY/SELL/BUY_LIMIT 等。
        返回 ticket。
        """
        self._validate_volume(symbol, volume)
        self.connect()
        request = {
            "action": mt5.TRADE_ACTION_DEAL if price is None else mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price
            if price
            else (
                mt5.symbol_info_tick(symbol).ask
                if order_type in (mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_BUY_LIMIT)
                else mt5.symbol_info_tick(symbol).bid
            ),
            "sl": sl or 0.0,
            "tp": tp or 0.0,
            "deviation": deviation,
            "magic": magic,
            "comment": comment,
            "type_filling": mt5.ORDER_FILLING_FOK,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            raise MT5TradingClientError(f"Order send failed: {result and result.comment}")
        return result.order

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

    def estimate_margin(self, symbol: str, volume: float, side: str, price: Optional[float] = None) -> float:
        """
        预估开仓所需保证金，便于上层风控。
        """
        self._validate_volume(symbol, volume)
        self.connect()
        order_type = self._side_to_order_type(side)
        tick = mt5.symbol_info_tick(symbol)
        price = price or (tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid)
        margin = mt5.order_calc_margin(order_type, symbol, volume, price)
        if margin is None:
            raise MT5TradingClientError(f"Failed to calc margin: {mt5.last_error()}")
        return margin

    def close_position(self, ticket: int, deviation: int = 20, comment: str = "") -> bool:
        self.connect()
        position = mt5.positions_get(ticket=ticket)
        if not position:
            raise MT5TradingClientError(f"Position {ticket} not found")
        pos = position[0]
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
            "comment": comment or "close",
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            raise MT5TradingClientError(f"Close failed: {result and result.comment}")
        return True

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
