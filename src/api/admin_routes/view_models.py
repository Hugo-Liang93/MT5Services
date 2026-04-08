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
    name: str
    category: Optional[str] = None
    preferred_scopes: List[str] = Field(default_factory=list)
    required_indicators: List[str] = Field(default_factory=list)
    regime_affinity: Dict[str, float] = Field(default_factory=dict)
    session_performance: Optional[Dict[str, Any]] = None


class PipelineStatsView(BaseModel):
    model_config = {"extra": "allow"}
