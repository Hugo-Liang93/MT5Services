"""
Pydantic 模型：用于 API 输入/输出的统一定义。
AI友好接口优化 - 扩展ApiResponse模型
"""

from __future__ import annotations

from datetime import datetime
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
    indicators: Optional[Dict[str, float]] = None


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
    """AI友好的API响应格式
    
    扩展原有ApiResponse，添加错误信息和元数据字段
    便于AI agent解析和处理
    """
    success: bool = True
    data: Optional[T] = None
    error: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    
    @classmethod
    def success_response(cls, data: T, metadata: Optional[Dict[str, Any]] = None) -> "ApiResponse[T]":
        """创建成功响应
        
        Args:
            data: 响应数据
            metadata: 元数据，可包含时间戳、数据来源等信息
            
        Returns:
            ApiResponse实例
        """
        default_metadata = {
            "timestamp": datetime.now().isoformat(),
            "data_source": "mt5_realtime",
            "data_freshness": "fresh"
        }
        if metadata:
            default_metadata.update(metadata)
        
        return cls(
            success=True,
            data=data,
            error=None,
            metadata=default_metadata
        )
    
    @classmethod
    def error_response(cls, 
                      error_code: str, 
                      error_message: str, 
                      suggested_action: Optional[str] = None,
                      details: Optional[Dict[str, Any]] = None) -> "ApiResponse[None]":
        """创建错误响应
        
        Args:
            error_code: 错误代码，AI可识别的标识
            error_message: 错误描述，人类可读
            suggested_action: 建议AI执行的动作
            details: 错误详情，用于调试
            
        Returns:
            ApiResponse实例
        """
        return cls(
            success=False,
            data=None,
            error={
                "code": error_code,
                "message": error_message,
                "suggested_action": suggested_action,
                "details": details or {}
            },
            metadata={
                "timestamp": datetime.now().isoformat(),
                "data_source": "error"
            }
        )