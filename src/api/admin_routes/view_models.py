from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class IndicatorConfigItem(BaseModel):
    name: str
    display: bool = False

    model_config = {"extra": "allow"}


class IndicatorsConfigView(BaseModel):
    indicators: List[IndicatorConfigItem] = Field(default_factory=list)
    total_count: int = 0
    display_count: int = 0
    intrabar_indicators: List[str] = Field(default_factory=list)


class StrategyPipelineItem(BaseModel):
    name: str
    category: Optional[str] = None
    preferred_scopes: List[str] = Field(default_factory=list)
    required_indicators: List[str] = Field(default_factory=list)
    regime_affinity: Dict[str, float] = Field(default_factory=dict)
    session_multiplier: float = 1.0
    session_stats: Optional[Dict[str, Any]] = None

    model_config = {"extra": "allow"}


class ConfidencePipelineView(BaseModel):
    symbol: str
    timeframe: str
    regime: Dict[str, Any] = Field(default_factory=dict)
    calibrator: Dict[str, Any] = Field(default_factory=dict)
    strategies: List[StrategyPipelineItem] = Field(default_factory=list)


class StrategySessionDetailView(BaseModel):
    """策略详情视图。

    extra='allow' + 显式 htf_policy：避免双重 schema 裁剪
    （build_strategy_detail() → StrategyDetail → model_dump → 此 view），
    structured 策略必须声明 htf_policy 才能 live；详情接口必须暴露。
    """

    model_config = {"extra": "allow"}

    name: str
    category: Optional[str] = None
    preferred_scopes: List[str] = Field(default_factory=list)
    required_indicators: List[str] = Field(default_factory=list)
    regime_affinity: Dict[str, float] = Field(default_factory=dict)
    htf_policy: Optional[str] = None
    session_performance: Optional[Dict[str, Any]] = None


class PipelineStatsView(BaseModel):
    model_config = {"extra": "allow"}
