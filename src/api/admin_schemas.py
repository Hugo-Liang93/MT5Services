"""Admin 仪表板专用 Pydantic Schema 模型。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# ── 仪表板概览子模型 ─────────────────────────────────────────


class SystemStatusSnapshot(BaseModel):
    """系统状态快照。"""

    status: str = Field(description="healthy / warning / critical")
    uptime_seconds: Optional[float] = None
    started_at: Optional[str] = None
    ready: bool = False
    phase: str = "unknown"


class ExecutorSnapshot(BaseModel):
    """交易执行器状态快照。"""

    enabled: bool = False
    circuit_open: bool = False
    consecutive_failures: int = 0
    execution_count: int = 0
    last_execution_at: Optional[str] = None
    pending_entries_count: int = 0


class DashboardOverview(BaseModel):
    """仪表板首屏聚合数据。"""

    system: SystemStatusSnapshot
    account: Dict[str, Any] = Field(default_factory=dict)
    positions: Dict[str, Any] = Field(default_factory=dict)
    signals: Dict[str, Any] = Field(default_factory=dict)
    executor: ExecutorSnapshot
    storage: Dict[str, Any] = Field(default_factory=dict)
    indicators: Dict[str, Any] = Field(default_factory=dict)


# ── 配置查看 ─────────────────────────────────────────────────


class ConfigView(BaseModel):
    """全配置聚合视图。"""

    effective: Dict[str, Any] = Field(default_factory=dict)
    provenance: Dict[str, Dict[str, str]] = Field(default_factory=dict)
    files: List[str] = Field(default_factory=list)


# ── 绩效报表 ─────────────────────────────────────────────────


class StrategyPerformanceReport(BaseModel):
    """策略绩效聚合报表。"""

    session_ranking: List[Dict[str, Any]] = Field(default_factory=list)
    session_summary: Dict[str, Any] = Field(default_factory=dict)
    historical_winrates: List[Dict[str, Any]] = Field(default_factory=list)
    calibrator: Dict[str, Any] = Field(default_factory=dict)


class StrategyDetail(BaseModel):
    """策略完整信息。"""

    name: str
    category: Optional[str] = None
    preferred_scopes: List[str] = Field(default_factory=list)
    required_indicators: List[str] = Field(default_factory=list)
    regime_affinity: Dict[str, float] = Field(default_factory=dict)
