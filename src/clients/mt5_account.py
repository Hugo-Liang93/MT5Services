"""
MT5 账户/持仓/订单只读封装。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from src.clients.base import MT5BaseClient, MT5BaseError, mt5


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


class MT5AccountClientError(MT5BaseError):
    pass


class MT5AccountClient(MT5BaseClient):
    def account_info(self) -> AccountInfo:
        self.connect()
        info = mt5.account_info()
        if info is None:
            raise MT5AccountClientError(f"Failed to get account info: {mt5.last_error()}")
        return AccountInfo(
            login=info.login,
            balance=info.balance,
            equity=info.equity,
            margin=info.margin,
            margin_free=info.margin_free,
            leverage=info.leverage,
            currency=info.currency,
        )

    def positions(self, symbol: Optional[str] = None) -> List[Position]:
        self.connect()
        sel = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if sel is None:
            raise MT5AccountClientError(f"Failed to get positions: {mt5.last_error()}")
        return [
            Position(
                ticket=p.ticket,
                symbol=p.symbol,
                volume=p.volume,
                price_open=p.price_open,
                sl=p.sl,
                tp=p.tp,
                time=self._to_tz(datetime.fromtimestamp(p.time, tz=timezone.utc)),
                type=p.type,
                magic=p.magic,
                comment=p.comment,
            )
            for p in sel
        ]

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
                time=self._to_tz(datetime.fromtimestamp(o.time_setup, tz=timezone.utc)),
                type=o.type,
                magic=o.magic,
                comment=o.comment,
            )
            for o in sel
        ]
