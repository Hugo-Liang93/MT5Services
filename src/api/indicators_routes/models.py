from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class IndicatorValue(BaseModel):
    name: str = Field(..., description="指标名称")
    value: Dict[str, Any] = Field(..., description="指标值字典")
    timestamp: datetime = Field(..., description="计算时间")
    bar_time: Optional[datetime] = Field(None, description="对应的K线时间")
    cache_hit: bool = Field(False, description="是否来自缓存")
    incremental: bool = Field(False, description="是否使用增量计算")
    compute_time_ms: float = Field(0.0, description="计算耗时（毫秒）")


class IndicatorRequest(BaseModel):
    symbol: str = Field(..., description="交易品种")
    timeframe: str = Field(..., description="时间框架")
    indicators: List[str] = Field(..., description="指标名称列表")


class IndicatorResponse(BaseModel):
    symbol: str = Field(..., description="交易品种")
    timeframe: str = Field(..., description="时间框架")
    indicators: List[IndicatorValue] = Field(..., description="指标值列表")
    total_compute_time_ms: float = Field(0.0, description="总计算耗时（毫秒）")
