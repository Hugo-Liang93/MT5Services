from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class FlexibleSignalView(BaseModel):
    model_config = ConfigDict(extra="allow")


class SignalRuntimeSummaryView(FlexibleSignalView):
    status: str
    running: bool
    target_count: int = 0
    trigger_mode: Dict[str, Any] = Field(default_factory=dict)
    strategy_sessions: Dict[str, Any] = Field(default_factory=dict)
    strategy_scopes: Dict[str, Any] = Field(default_factory=dict)
    market_structure: Dict[str, Any] = Field(default_factory=dict)
    queues: Dict[str, Any] = Field(default_factory=dict)
    last_error: Optional[str] = None
    strategy_capability_reconciliation: Dict[str, Any] = Field(default_factory=dict)
    strategy_capability_execution_plan: Dict[str, Any] = Field(default_factory=dict)


class TrackedPositionsView(FlexibleSignalView):
    count: int = 0
    items: List[Dict[str, Any]] = Field(default_factory=list)
    manager: Dict[str, Any] = Field(default_factory=dict)


class RegimeReportView(FlexibleSignalView):
    symbol: str
    timeframe: str


class MarketStructureView(FlexibleSignalView):
    symbol: str
    timeframe: str


class HTFCacheStatusView(FlexibleSignalView):
    pass


class CalibratorStatusView(FlexibleSignalView):
    pass


class StrategyDiagnosticsView(FlexibleSignalView):
    pass


class SignalMonitoringQualityView(FlexibleSignalView):
    symbol: str
    timeframe: str
    regime: Dict[str, Any] = Field(default_factory=dict)
    quality: Dict[str, Any] = Field(default_factory=dict)


class StrategyWinrateView(FlexibleSignalView):
    pass


class IntrabarSLOPoint(BaseModel):
    timestamp: str
    value: float
    alert_level: str | None = None


class IntrabarSLOWindowView(FlexibleSignalView):
    component: str
    limit: int
    drop_rate: list[IntrabarSLOPoint]
    queue_age_ms_p95: list[IntrabarSLOPoint]
    to_decision_latency_ms_p95: list[IntrabarSLOPoint]
