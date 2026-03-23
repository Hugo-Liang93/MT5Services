"""
MT5 账户/持仓/订单只读封装。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

from src.clients.base import MT5BaseClient, mt5
from src.clients.base import MT5TradeError


@dataclass
class AccountInfo:
    login: int
    balance: float
    equity: float
    margin: float
    margin_free: float
    leverage: int
    currency: str


@dataclass
class Position:
    ticket: int
    symbol: str
    volume: float
    price_open: float
    sl: float
    tp: float
    time: datetime
    type: int
    magic: int
    comment: str


@dataclass
class Order:
    ticket: int
    symbol: str
    volume: float
    price_open: float
    price_current: float
    sl: float
    tp: float
    time: datetime
    type: int
    magic: int
    comment: str


class MT5AccountClientError(MT5TradeError):
    pass


class MT5AccountClient(MT5BaseClient):
    # 对只读账户/持仓查询进行短 TTL 缓存，减少高并发下的 MT5 API 序列化压力。
    # 缓存仅用于读路径；写操作（下单/平仓）会主动使缓存失效。
    _ACCOUNT_INFO_TTL = 3.0   # seconds
    _POSITIONS_TTL = 3.0      # seconds

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._account_info_cache: Optional[Tuple[float, AccountInfo]] = None
        self._positions_cache: dict[Optional[str], Tuple[float, List[Position]]] = {}

    def invalidate_cache(self) -> None:
        """下单/平仓后调用，使持仓和账户缓存失效。"""
        self._account_info_cache = None
        self._positions_cache.clear()

    def account_info(self) -> AccountInfo:
        cached = self._account_info_cache
        if cached is not None and (time.monotonic() - cached[0]) < self._ACCOUNT_INFO_TTL:
            return cached[1]
        self.connect()
        info = mt5.account_info()
        if info is None:
            raise MT5AccountClientError(f"Failed to get account info: {mt5.last_error()}")
        result = AccountInfo(
            login=info.login,
            balance=info.balance,
            equity=info.equity,
            margin=info.margin,
            margin_free=info.margin_free,
            leverage=info.leverage,
            currency=info.currency,
        )
        self._account_info_cache = (time.monotonic(), result)
        return result

    def positions(self, symbol: Optional[str] = None) -> List[Position]:
        cached = self._positions_cache.get(symbol)
        if cached is not None and (time.monotonic() - cached[0]) < self._POSITIONS_TTL:
            return cached[1]
        self.connect()
        sel = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if sel is None:
            raise MT5AccountClientError(f"Failed to get positions: {mt5.last_error()}")
        result = [
            Position(
                ticket=p.ticket,
                symbol=p.symbol,
                volume=p.volume,
                price_open=p.price_open,
                sl=p.sl,
                tp=p.tp,
                time=self._parse_server_timestamp(p.time),
                type=p.type,
                magic=p.magic,
                comment=p.comment,
            )
            for p in sel
        ]
        self._positions_cache[symbol] = (time.monotonic(), result)
        return result

    def orders(self, symbol: Optional[str] = None) -> List[Order]:
        self.connect()
        sel = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
        if sel is None:
            raise MT5AccountClientError(f"Failed to get orders: {mt5.last_error()}")
        return [
            Order(
                ticket=o.ticket,
                symbol=o.symbol,
                volume=o.volume_initial,
                price_open=o.price_open,
                price_current=o.price_current,
                sl=o.sl,
                tp=o.tp,
                time=self._parse_server_timestamp(o.time_setup),
                type=o.type,
                magic=o.magic,
                comment=o.comment,
            )
            for o in sel
        ]
