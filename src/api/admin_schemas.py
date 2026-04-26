"""Admin 仪表板专用 Pydantic Schema 模型。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

# ── 仪表板概览子模型 ─────────────────────────────────────────


class SystemStatusSnapshot(BaseModel):
    """系统状态快照。"""

    status: str = Field(description="healthy / warning / critical")
    uptime_seconds: Optional[float] = None
    started_at: Optional[str] = None
    ready: bool = False
    phase: str = "unknown"


class ExecutorSnapshot(BaseModel):
    """交易执行器状态快照。

    显式声明 readmodel.trade_executor_summary() 暴露的核心诊断字段；
    extra='allow' 允许 readmodel 后续添加字段时透传给前端，避免 schema
    与运行时 readmodel 漂移导致诊断信息被静默裁剪（同 §0q 元层教训）。
    """

    model_config = ConfigDict(extra="allow")

    # 状态机字段（前端用以判断 disabled / delegated / critical / blocked）
    status: Optional[str] = None
    configured: bool = False
    armed: bool = False
    running: bool = False
    enabled: bool = False
    # 熔断 + 计数
    circuit_open: bool = False
    consecutive_failures: int = 0
    execution_count: int = 0
    # 时间线 + 错误
    last_execution_at: Optional[str] = None
    last_error: Optional[str] = None
    last_risk_block: Optional[str] = None
    # 信号统计 / 执行门控 / 待挂单 / 最近执行
    signals: Dict[str, Any] = Field(default_factory=dict)
    execution_gate: Dict[str, Any] = Field(default_factory=dict)
    execution_quality: Dict[str, Any] = Field(default_factory=dict)
    config: Dict[str, Any] = Field(default_factory=dict)
    pending_entries_count: int = 0
    pending_entries: Dict[str, Any] = Field(default_factory=dict)
    recent_executions: List[Dict[str, Any]] = Field(default_factory=list)
    execution_scope: Optional[str] = None


class DashboardOverview(BaseModel):
    """仪表板首屏聚合数据。

    显式声明 readmodel.dashboard_overview() 已知顶层区块；extra='allow'
    允许 readmodel 后续添加新区块（如未来的 risk_state / margin_summary）
    时透传给前端，避免 7 字段 schema 把 readmodel 的 11+ 区块裁掉
    （历史 bug：trading_state / account_risk / validation /
    external_dependencies 长期被静默裁；参 §0q）。
    """

    model_config = ConfigDict(extra="allow")

    system: SystemStatusSnapshot
    account: Dict[str, Any] = Field(default_factory=dict)
    positions: Dict[str, Any] = Field(default_factory=dict)
    trading_state: Dict[str, Any] = Field(default_factory=dict)
    account_risk: Dict[str, Any] = Field(default_factory=dict)
    signals: Dict[str, Any] = Field(default_factory=dict)
    executor: ExecutorSnapshot
    validation: Dict[str, Any] = Field(default_factory=dict)
    external_dependencies: Dict[str, Any] = Field(default_factory=dict)
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
    """策略完整信息。

    extra='allow' + 显式 htf_policy 字段：structured strategy 必须声明
    htf_policy（HARD_GATE / SOFT_GATE / SOFT_BONUS / NONE，参
    docs/signal-system.md），SignalModule.describe_strategy() 已暴露
    该字段，schema 旧实现裁剪导致前端拿不到完整策略契约。
    """

    model_config = ConfigDict(extra="allow")

    name: str
    category: Optional[str] = None
    preferred_scopes: List[str] = Field(default_factory=list)
    required_indicators: List[str] = Field(default_factory=list)
    regime_affinity: Dict[str, float] = Field(default_factory=dict)
    htf_policy: Optional[str] = None
