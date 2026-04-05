"""
MT5 交易封装：下单/平仓等写操作。
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

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
    def _normalize_comment(comment: str, default: str) -> str:
        raw = str(comment or "").strip()
        if not raw:
            raw = default
        normalized = re.sub(r"[^A-Za-z0-9._ -]+", "_", raw)
        normalized = normalized.strip() or default
        return normalized[:27]

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

    @staticmethod
    def _is_unsupported_filling_response(result: Any) -> bool:
        comment = str(getattr(result, "comment", "") or "").strip().lower()
        return "unsupported filling mode" in comment

    def _candidate_filling_modes(self, symbol: str, preferred: Optional[int] = None) -> list[int]:
        candidates: list[int] = []
        if preferred is not None:
            candidates.append(preferred)

        info = mt5.symbol_info(symbol)
        symbol_mode = getattr(info, "filling_mode", None) if info is not None else None
        if symbol_mode is not None:
            candidates.append(int(symbol_mode))

        for mode_name in ("ORDER_FILLING_RETURN", "ORDER_FILLING_IOC", "ORDER_FILLING_FOK"):
            mode = getattr(mt5, mode_name, None)
            if mode is not None:
                candidates.append(int(mode))

        unique: list[int] = []
        seen = set()
        for mode in candidates:
            if mode in seen:
                continue
            seen.add(mode)
            unique.append(mode)
        return unique

    def _send_order_with_supported_fillings(self, request: dict[str, Any]) -> Any:
        symbol = str(request.get("symbol") or "")
        preferred = request.get("type_filling")
        last_result = None
        for fill_mode in self._candidate_filling_modes(symbol, preferred):
            req = dict(request)
            req["type_filling"] = fill_mode
            result = mt5.order_send(req)
            last_result = result
            if result is not None and not self._is_unsupported_filling_response(result):
                return result
        return last_result

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
            "comment": self._normalize_comment(comment, "trade"),
            "type_filling": getattr(mt5, "ORDER_FILLING_RETURN", getattr(mt5, "ORDER_FILLING_IOC", getattr(mt5, "ORDER_FILLING_FOK", 0))),
        }
        result = self._send_order_with_supported_fillings(request)
        success_codes = {mt5.TRADE_RETCODE_DONE}
        if is_pending:
            success_codes.add(getattr(mt5, "TRADE_RETCODE_PLACED", mt5.TRADE_RETCODE_DONE))
        if result is None or result.retcode not in success_codes:
            raise MT5TradingClientError(f"Order send failed: {result and result.comment}")
        ticket = int(getattr(result, "order", 0) or getattr(result, "deal", 0) or 0)
        raw_fill_price = getattr(result, "price", None)
        try:
            fill_price = float(raw_fill_price) if raw_fill_price is not None else float(request_price)
        except (TypeError, ValueError):
            fill_price = float(request_price)
        return {
            "ticket": ticket,
            "order": int(getattr(result, "order", 0) or 0),
            "deal": int(getattr(result, "deal", 0) or 0),
            "retcode": int(result.retcode),
            "comment": getattr(result, "comment", ""),
            "symbol": symbol,
            "volume": volume,
            "price": request_price,
            "requested_price": request_price,
            "fill_price": fill_price,
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
        order_type = self.side_and_kind_to_order_type(side, "market")
        tick = mt5.symbol_info_tick(symbol)
        price = price or (tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid)
        margin = mt5.order_calc_margin(order_type, symbol, volume, price)
        if margin is None:
            raise MT5TradingClientError(f"Failed to calc margin: {mt5.last_error()}")
        return margin

    def check_broker_constraints(
        self,
        *,
        symbol: str,
        side: str,
        request_price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """检查 broker 层面的交易约束，返回结构化检查结果列表。

        每个结果为 ``{"name": str, "passed": bool, "message": str}``。
        不抛出异常——由调用方决定如何处理 ``passed=False`` 的条目。
        """
        self.connect()
        checks: list[dict[str, Any]] = []

        info = mt5.symbol_info(symbol)
        if info is None:
            checks.append({
                "name": "symbol_available",
                "passed": False,
                "message": f"Symbol {symbol} not found or not available",
            })
            return checks

        # ── trade_mode 检查 ─────────────────────────────────────────────
        raw_mode = getattr(info, "trade_mode", None)
        trade_mode = int(raw_mode) if raw_mode is not None else 4
        is_buy = str(side).lower() in ("buy", "long")
        # 0=DISABLED, 1=LONGONLY, 2=SHORTONLY, 3=CLOSEONLY, 4=FULL
        if trade_mode == 0:
            checks.append({
                "name": "trade_mode",
                "passed": False,
                "message": f"Trading disabled for {symbol} (trade_mode=DISABLED)",
            })
        elif trade_mode == 3:
            checks.append({
                "name": "trade_mode",
                "passed": False,
                "message": f"Only close operations allowed for {symbol} (trade_mode=CLOSEONLY)",
            })
        elif trade_mode == 1 and not is_buy:
            checks.append({
                "name": "trade_mode",
                "passed": False,
                "message": f"Only buy operations allowed for {symbol} (trade_mode=LONGONLY)",
            })
        elif trade_mode == 2 and is_buy:
            checks.append({
                "name": "trade_mode",
                "passed": False,
                "message": f"Only sell operations allowed for {symbol} (trade_mode=SHORTONLY)",
            })
        else:
            checks.append({"name": "trade_mode", "passed": True, "message": "ok"})

        # ── stops_level 检查：SL/TP 最小距离 ─────────────────────────────
        raw_stops = getattr(info, "trade_stops_level", None)
        stops_level = int(raw_stops) if raw_stops is not None else 0
        raw_point = getattr(info, "point", None)
        point = float(raw_point) if raw_point is not None else 0.0

        # 确定参考价格
        ref_price = request_price
        if ref_price is None:
            tick = mt5.symbol_info_tick(symbol)
            if tick is not None:
                ref_price = tick.ask if is_buy else tick.bid

        if stops_level > 0 and point > 0 and ref_price is not None and ref_price > 0:
            min_distance = stops_level * point

            if sl is not None:
                sl_distance = abs(ref_price - sl)
                if sl_distance < min_distance - point * 0.5:
                    checks.append({
                        "name": "stops_level_sl",
                        "passed": False,
                        "message": (
                            f"SL too close: distance {sl_distance:.{info.digits}f} "
                            f"< minimum {min_distance:.{info.digits}f} "
                            f"({stops_level} points)"
                        ),
                    })
                else:
                    checks.append({"name": "stops_level_sl", "passed": True, "message": "ok"})

            if tp is not None:
                tp_distance = abs(ref_price - tp)
                if tp_distance < min_distance - point * 0.5:
                    checks.append({
                        "name": "stops_level_tp",
                        "passed": False,
                        "message": (
                            f"TP too close: distance {tp_distance:.{info.digits}f} "
                            f"< minimum {min_distance:.{info.digits}f} "
                            f"({stops_level} points)"
                        ),
                    })
                else:
                    checks.append({"name": "stops_level_tp", "passed": True, "message": "ok"})

        # ── freeze_level 提示（仅 warning，不阻断）──────────────────────
        raw_freeze = getattr(info, "trade_freeze_level", None)
        freeze_level = int(raw_freeze) if raw_freeze is not None else 0
        if freeze_level > 0 and point > 0:
            freeze_distance = freeze_level * point
            checks.append({
                "name": "freeze_level",
                "passed": True,
                "message": (
                    f"Freeze level = {freeze_level} points "
                    f"({freeze_distance:.{info.digits}f}) — "
                    f"orders within this distance of SL/TP cannot be modified"
                ),
            })

        return checks

    def validate_trade_request(
        self,
        *,
        symbol: str,
        volume: float,
        side: str,
        order_kind: str = "market",
        price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> dict:
        self.connect()
        self._validate_volume(symbol, volume)
        order_type = self.side_and_kind_to_order_type(side, order_kind)
        is_pending = order_type in self._pending_order_types()
        request_price = price
        if request_price is None:
            if is_pending:
                raise MT5TradingClientError("Pending orders require an explicit price")
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                raise MT5TradingClientError(f"Failed to get tick for {symbol}: {mt5.last_error()}")
            request_price = tick.ask if order_type in self._buy_order_types() else tick.bid
        self._validate_protection_levels(
            order_type=order_type,
            request_price=request_price,
            sl=sl,
            tp=tp,
        )
        # Broker 约束检查：stops_level / trade_mode 等
        broker_checks = self.check_broker_constraints(
            symbol=symbol,
            side=side,
            request_price=request_price,
            sl=sl,
            tp=tp,
        )
        failed = [c for c in broker_checks if not c["passed"]]
        if failed:
            reasons = "; ".join(c["message"] for c in failed)
            raise MT5TradingClientError(f"Broker constraint violated: {reasons}")

        return {
            "order_type": order_type,
            "request_price": request_price,
            "pending": is_pending,
            "broker_checks": broker_checks,
        }

    def close_position(
        self,
        ticket: int,
        deviation: int = 20,
        comment: str = "",
        volume: Optional[float] = None,
    ) -> bool:
        self.connect()
        pos = None
        for _attempt in range(3):
            position = mt5.positions_get(ticket=ticket)
            if position:
                pos = position[0]
                break
            time.sleep(0.1)
        if pos is None:
            raise MT5TradingClientError(f"Position {ticket} not found")
        close_volume = float(volume if volume is not None else pos.volume)
        if close_volume <= 0 or close_volume > pos.volume:
            raise MT5TradingClientError(f"Close volume {close_volume} invalid for position {ticket}")

        last_error: Any = None
        for _attempt in range(3):
            tick = mt5.symbol_info_tick(pos.symbol)
            if tick is None:
                last_error = mt5.last_error()
                time.sleep(0.1)
                continue
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
                "comment": self._normalize_comment(comment, "close"),
                "type_filling": getattr(mt5, "ORDER_FILLING_RETURN", getattr(mt5, "ORDER_FILLING_IOC", getattr(mt5, "ORDER_FILLING_FOK", 0))),
            }
            result = self._send_order_with_supported_fillings(request)
            if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                return True
            if result is not None:
                error_comment = getattr(result, "comment", None)
                raise MT5TradingClientError(f"Close failed: {error_comment}")
            # order_send returned None — position may have been closed by SL/TP
            # between the positions_get check and the order_send call.
            pos_recheck = mt5.positions_get(ticket=ticket)
            if not pos_recheck:
                return True
            last_error = mt5.last_error()
            time.sleep(0.1)
        raise MT5TradingClientError(f"Close failed: {last_error}")

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
                    "comment": self._normalize_comment(comment, "close_all"),
                    "type_filling": getattr(mt5, "ORDER_FILLING_RETURN", getattr(mt5, "ORDER_FILLING_IOC", getattr(mt5, "ORDER_FILLING_FOK", 0))),
                }
                result = self._send_order_with_supported_fillings(request)
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
        ticket: Optional[int] = None,
        symbol: Optional[str] = None,
        magic: Optional[int] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> dict:
        """
        批量修改持仓的止盈/止损。
        """
        self.connect()
        if ticket is not None:
            positions = mt5.positions_get(ticket=int(ticket))
        else:
            positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if positions is None:
            raise MT5TradingClientError(f"Failed to get positions: {mt5.last_error()}")
        targets = [
            p
            for p in positions
            if (ticket is None or int(getattr(p, "ticket", 0) or 0) == int(ticket))
            and (magic is None or p.magic == magic)
            and (symbol is None or p.symbol == symbol)
        ]
        modified, failed = [], []
        if ticket is not None and not targets:
            return {
                "modified": [],
                "failed": [{"ticket": int(ticket), "symbol": symbol, "error": "position_not_found"}],
            }
        for p in targets:
            req = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": p.ticket,
                "symbol": p.symbol,
                "sl": sl if sl is not None else p.sl,
                "tp": tp if tp is not None else p.tp,
            }
            result = mt5.order_send(req)
            retcode = int(result.retcode) if result is not None else -1
            comment = str(getattr(result, "comment", "") or "")
            if result is None or retcode != mt5.TRADE_RETCODE_DONE:
                failed.append({
                    "ticket": p.ticket, "symbol": p.symbol,
                    "error": comment, "retcode": retcode,
                })
            else:
                modified.append({
                    "ticket": p.ticket, "retcode": retcode, "comment": comment,
                })
        return {"modified": modified, "failed": failed}

    def get_position_close_details(
        self,
        ticket: int,
        *,
        symbol: Optional[str] = None,
        lookback_days: int = 7,
    ) -> Optional[dict[str, Any]]:
        self.connect()
        end_time = self._server_now()
        start_time = end_time - timedelta(days=max(1, int(lookback_days)))
        deals = mt5.history_deals_get(start_time, end_time)
        if deals is None:
            return None

        exit_entries = {
            getattr(mt5, "DEAL_ENTRY_OUT", None),
            getattr(mt5, "DEAL_ENTRY_OUT_BY", None),
        }
        matched: list[Any] = []
        for deal in deals:
            position_id = getattr(deal, "position_id", None)
            if position_id is None:
                position_id = getattr(deal, "position", None)
            if int(position_id or 0) != int(ticket):
                continue
            deal_symbol = str(getattr(deal, "symbol", "") or "")
            if symbol and deal_symbol and deal_symbol != symbol:
                continue
            entry = getattr(deal, "entry", None)
            if exit_entries and entry not in exit_entries and entry is not None:
                continue
            matched.append(deal)

        if not matched:
            return None

        matched.sort(
            key=lambda deal: (
                int(getattr(deal, "time_msc", 0) or 0),
                int(getattr(deal, "time", 0) or 0),
                int(getattr(deal, "ticket", 0) or 0),
            )
        )
        deal = matched[-1]
        close_time_msc = getattr(deal, "time_msc", None)
        close_time = (
            self._parse_server_timestamp_msc(int(close_time_msc))
            if close_time_msc
            else self._parse_server_timestamp(float(getattr(deal, "time", 0) or 0))
        )
        return {
            "ticket": int(ticket),
            "deal_id": int(getattr(deal, "ticket", 0) or 0),
            "symbol": str(getattr(deal, "symbol", "") or symbol or ""),
            "close_price": float(getattr(deal, "price", 0.0) or 0.0),
            "closed_at": close_time.isoformat(),
            "profit": float(getattr(deal, "profit", 0.0) or 0.0),
            "commission": float(getattr(deal, "commission", 0.0) or 0.0),
            "swap": float(getattr(deal, "swap", 0.0) or 0.0),
            "fee": float(getattr(deal, "fee", 0.0) or 0.0),
            "volume": float(getattr(deal, "volume", 0.0) or 0.0),
            "entry": getattr(deal, "entry", None),
        }

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
