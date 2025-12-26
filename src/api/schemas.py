"""
Pydantic 模型：用于 API 输入/输出的统一定义。
"""

from __future__ import annotations

from typing import Optional, TypeVar, Generic, List, Dict, Any

from pydantic.generics import GenericModel

from pydantic import BaseModel


class QuoteModel(BaseModel):
    symbol: str
    bid: float
    ask: float
    last: float
    volume: float
    time: str


class TickModel(BaseModel):
    symbol: str
    price: float
    volume: float
    time: str


class OHLCModel(BaseModel):
    symbol: str
    timeframe: str
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class AccountInfoModel(BaseModel):
    login: int
    balance: float
    equity: float
    margin: float
    margin_free: float
    leverage: int
    currency: str


class PositionModel(BaseModel):
    ticket: int
    symbol: str
    volume: float
    price_open: float
    sl: float
    tp: float
    time: str
    type: int
    magic: int
    comment: str


class OrderModel(BaseModel):
    ticket: int
    symbol: str
    volume: float
    price_open: float
    price_current: float
    sl: float
    tp: float
    time: str
    type: int
    magic: int
    comment: str


class TradeRequest(BaseModel):
    symbol: str
    volume: float
    side: str
    price: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    deviation: int = 20
    comment: str = ""
    magic: int = 0


class CloseRequest(BaseModel):
    ticket: int
    deviation: int = 20
    comment: str = ""


class CloseAllRequest(BaseModel):
    symbol: Optional[str] = None
    magic: Optional[int] = None
    side: Optional[str] = None
    deviation: int = 20
    comment: str = "close_all"

class CancelOrdersRequest(BaseModel):
    symbol: Optional[str] = None
    magic: Optional[int] = None

class EstimateMarginRequest(BaseModel):
    symbol: str
    volume: float
    side: str
    price: Optional[float] = None


class ModifyOrdersRequest(BaseModel):
    symbol: Optional[str] = None
    magic: Optional[int] = None
    sl: Optional[float] = None
    tp: Optional[float] = None


class ModifyPositionsRequest(BaseModel):
    symbol: Optional[str] = None
    magic: Optional[int] = None
    sl: Optional[float] = None
    tp: Optional[float] = None


class SymbolInfoModel(BaseModel):
    symbol: str
    description: str
    digits: int
    point: float
    trade_contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    margin_initial: float
    margin_maintenance: float
    tick_value: float
    tick_size: float


T = TypeVar("T")


class ApiResponse(GenericModel, Generic[T]):
    success: bool = True
    data: T
