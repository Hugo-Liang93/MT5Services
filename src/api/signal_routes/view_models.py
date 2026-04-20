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


class StrategyAuditEntryView(FlexibleSignalView):
    """单个策略的 admission/conflict/winrate 聚合（backlog P0.3）。

    **重要语义**（参考 docs/signal-system.md 'Signal 持久化语义'）：
    计数字段（signals/hold_count/blocked_count/...）基于 signal_events 表，
    该表只记录 state-transition signal（状态发生变化的 bar），不是每 bar 的评估。
    因此绝对值低估总评估次数，但各比例字段（hold_rate/blocked_rate/conflict_rate）
    反映**策略对新入场机会的决策分布**，是准确的。
    win_rate 来自 signal_outcomes 表，独立于该过滤，反映真实预测力。
    """

    strategy: str
    category: Optional[str] = None
    # state-transition signal 数（首次 buy/sell 或方向翻转），不是评估总次数
    signals: int = 0
    actionable_signals: int = 0
    hold_count: int = 0
    blocked_count: int = 0
    conflict_count: int = 0
    # 比例字段，反映新入场机会的决策分布（准确）
    hold_rate: float = 0.0
    blocked_rate: float = 0.0
    conflict_rate: float = 0.0
    avg_confidence: float = 0.0
    # win_rate 来自 signal_outcomes（N-bar 后实际价差），不受 state-transition 过滤影响
    win_rate: Optional[float] = None
    last_signal_at: Optional[str] = None
    recent_issue: Optional[str] = None
    status: str = "ok"
    warnings: List[str] = Field(default_factory=list)


class StrategyAuditView(FlexibleSignalView):
    """/v1/signals/diagnostics/strategy-audit 响应。"""

    rows_analyzed: int = 0
    scope: str = "confirmed"
    symbol: Optional[str] = None
    timeframe: Optional[str] = None
    thresholds: Dict[str, float] = Field(default_factory=dict)
    strategies: List[StrategyAuditEntryView] = Field(default_factory=list)


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
