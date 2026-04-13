from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class TradeControlStateView(FlexibleModel):
    auto_entry_enabled: Optional[bool] = None
    close_only_mode: Optional[bool] = None
    updated_at: Optional[str] = None
    reason: Optional[str] = None
    actor: Optional[str] = None
    action_id: Optional[str] = None
    audit_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    request_context: dict[str, Any] = Field(default_factory=dict)


class RuntimeModeSummaryView(FlexibleModel):
    status: Optional[str] = None
    current_mode: Optional[str] = None
    configured_mode: Optional[str] = None
    after_eod_action: Optional[str] = None
    auto_check_interval_seconds: Optional[float] = None
    last_transition_at: Optional[str] = None
    last_transition_reason: Optional[str] = None
    last_error: Optional[str] = None
    last_actor: Optional[str] = None
    last_action_id: Optional[str] = None
    last_audit_id: Optional[str] = None
    last_idempotency_key: Optional[str] = None
    last_request_context: dict[str, Any] = Field(default_factory=dict)
    components: dict[str, Any] = Field(default_factory=dict)


class CloseoutActionResultView(FlexibleModel):
    requested: list[int] = Field(default_factory=list)
    completed: list[int] = Field(default_factory=list)
    failed: list[dict[str, Any]] = Field(default_factory=list)
    error: Optional[str] = None


class RuntimeModeTransitionView(FlexibleModel):
    configured_action: Optional[str] = None
    target_mode: Optional[str] = None
    applied: bool = False
    reason: Optional[str] = None
    error: Optional[str] = None
    snapshot: Optional[dict[str, Any]] = None


class ExposureCloseoutResultView(FlexibleModel):
    positions: CloseoutActionResultView
    orders: CloseoutActionResultView
    remaining_positions: list[int] = Field(default_factory=list)
    remaining_orders: list[int] = Field(default_factory=list)
    completed: bool = False


class ExposureCloseoutSummaryView(FlexibleModel):
    status: str
    last_reason: Optional[str] = None
    last_comment: Optional[str] = None
    last_requested_at: Optional[str] = None
    last_completed_at: Optional[str] = None
    actor: Optional[str] = None
    action_id: Optional[str] = None
    audit_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    request_context: dict[str, Any] = Field(default_factory=dict)
    result: Optional[ExposureCloseoutResultView] = None
    runtime_mode_transition: Optional[RuntimeModeTransitionView] = None


class PendingOrderStateItemView(FlexibleModel):
    order_ticket: Optional[int] = None
    signal_id: Optional[str] = None
    status: Optional[str] = None
    symbol: Optional[str] = None


class PendingOrderStateListView(FlexibleModel):
    count: int
    status_counts: dict[str, int] = Field(default_factory=dict)
    items: list[PendingOrderStateItemView] = Field(default_factory=list)
    view: Optional[str] = None
    active_statuses: Optional[list[str]] = None


class ExecutionContextItemView(FlexibleModel):
    signal_id: Optional[str] = None
    symbol: Optional[str] = None
    timeframe: Optional[str] = None
    strategy: Optional[str] = None
    direction: Optional[str] = None
    source: Optional[str] = None


class ExecutionContextListView(FlexibleModel):
    count: int
    source_counts: dict[str, int] = Field(default_factory=dict)
    items: list[ExecutionContextItemView] = Field(default_factory=list)
    view: Optional[str] = None


class PositionRuntimeStateItemView(FlexibleModel):
    position_ticket: Optional[int] = None
    order_ticket: Optional[int] = None
    signal_id: Optional[str] = None
    status: Optional[str] = None
    symbol: Optional[str] = None


class PositionRuntimeStateListView(FlexibleModel):
    count: int
    status_counts: dict[str, int] = Field(default_factory=dict)
    items: list[PositionRuntimeStateItemView] = Field(default_factory=list)


class TradeStateAlertView(FlexibleModel):
    code: str
    severity: Optional[str] = None
    message: Optional[str] = None
    details: dict[str, Any] = Field(default_factory=dict)


class TradeStateAlertSummaryItemView(FlexibleModel):
    code: str
    status: Optional[str] = None
    severity: Optional[str] = None
    message: Optional[str] = None


class TradeStateAlertsView(FlexibleModel):
    status: str
    account_alias: Optional[str] = None
    alerts: list[TradeStateAlertView] = Field(default_factory=list)
    summary: list[TradeStateAlertSummaryItemView] = Field(default_factory=list)
    observed: dict[str, Any] = Field(default_factory=dict)


class TradePendingStateView(FlexibleModel):
    active: PendingOrderStateListView
    lifecycle: PendingOrderStateListView
    execution_contexts: ExecutionContextListView


class TradeStateSummaryView(FlexibleModel):
    trade_control: Optional[TradeControlStateView] = None
    runtime_mode: RuntimeModeSummaryView
    closeout: ExposureCloseoutSummaryView
    pending: TradePendingStateView
    positions: PositionRuntimeStateListView
    alerts: TradeStateAlertsView
    validation: dict[str, Any] = Field(default_factory=dict)


class TradeControlStatusView(FlexibleModel):
    trade_control: dict[str, Any] = Field(default_factory=dict)
    persisted_trade_control: Optional[TradeControlStateView] = None
    trading_state: TradeStateSummaryView
    executor: dict[str, Any] = Field(default_factory=dict)


class TradeDailySummaryView(FlexibleModel):
    pass


class TradeEntryStatusView(FlexibleModel):
    pass


class TradeCommandAuditView(FlexibleModel):
    pass


class TradeMutationResultView(FlexibleModel):
    accepted: bool
    status: str
    action_id: str
    command_id: Optional[str] = None
    audit_id: Optional[str] = None
    actor: Optional[str] = None
    reason: Optional[str] = None
    idempotency_key: Optional[str] = None
    request_context: dict[str, Any] = Field(default_factory=dict)
    message: Optional[str] = None
    error_code: Optional[str] = None
    recorded_at: Optional[str] = None
    effective_state: dict[str, Any] = Field(default_factory=dict)


class TradeControlUpdateView(FlexibleModel):
    accepted: bool
    status: str
    action_id: str
    command_id: Optional[str] = None
    audit_id: Optional[str] = None
    actor: Optional[str] = None
    reason: Optional[str] = None
    idempotency_key: Optional[str] = None
    request_context: dict[str, Any] = Field(default_factory=dict)
    message: Optional[str] = None
    error_code: Optional[str] = None
    recorded_at: Optional[str] = None
    effective_state: dict[str, Any] = Field(default_factory=dict)
    trade_control: dict[str, Any] = Field(default_factory=dict)
    executor: dict[str, Any] = Field(default_factory=dict)


class RuntimeModeUpdateView(FlexibleModel):
    accepted: bool
    status: str
    action_id: str
    command_id: Optional[str] = None
    audit_id: Optional[str] = None
    actor: Optional[str] = None
    reason: Optional[str] = None
    idempotency_key: Optional[str] = None
    request_context: dict[str, Any] = Field(default_factory=dict)
    message: Optional[str] = None
    error_code: Optional[str] = None
    recorded_at: Optional[str] = None
    effective_state: dict[str, Any] = Field(default_factory=dict)
    runtime_mode: RuntimeModeSummaryView
    trading_state: TradeStateSummaryView


class ExposureCloseoutActionView(FlexibleModel):
    accepted: bool
    status: str
    action_id: str
    command_id: Optional[str] = None
    audit_id: Optional[str] = None
    actor: Optional[str] = None
    reason: Optional[str] = None
    idempotency_key: Optional[str] = None
    request_context: dict[str, Any] = Field(default_factory=dict)
    message: Optional[str] = None
    error_code: Optional[str] = None
    recorded_at: Optional[str] = None
    effective_state: dict[str, Any] = Field(default_factory=dict)
    closeout: dict[str, Any] = Field(default_factory=dict)
    trading_state: dict[str, Any] = Field(default_factory=dict)


class TradeTraceIdentifiersView(FlexibleModel):
    signal_id: Optional[str] = None
    signal_ids: list[str] = Field(default_factory=list)
    request_ids: list[str] = Field(default_factory=list)
    trace_ids: list[str] = Field(default_factory=list)
    operation_ids: list[str] = Field(default_factory=list)
    order_tickets: list[int] = Field(default_factory=list)
    position_tickets: list[int] = Field(default_factory=list)


class TradeTraceAdmissionView(FlexibleModel):
    decision: Optional[str] = None
    stage: Optional[str] = None
    generated_at: Optional[str] = None
    reason_count: int = 0
    trace_id: Optional[str] = None
    signal_id: Optional[str] = None
    intent_id: Optional[str] = None
    command_id: Optional[str] = None
    action_id: Optional[str] = None


class TradeTraceSummaryView(FlexibleModel):
    stages: dict[str, str] = Field(default_factory=dict)
    pipeline_event_counts: dict[str, int] = Field(default_factory=dict)
    command_counts: dict[str, int] = Field(default_factory=dict)
    pending_status_counts: dict[str, int] = Field(default_factory=dict)
    position_status_counts: dict[str, int] = Field(default_factory=dict)
    admission: Optional[TradeTraceAdmissionView] = None
    status: Optional[str] = None
    started_at: Optional[str] = None
    last_event_at: Optional[str] = None
    event_count: Optional[int] = None
    last_stage: Optional[str] = None
    reason: Optional[str] = None


class TradeTraceTimelineEventView(FlexibleModel):
    id: str
    stage: str
    status: Optional[str] = None
    at: Optional[Any] = None
    source: Optional[str] = None
    summary: Optional[str] = None
    details: dict[str, Any] = Field(default_factory=dict)


class TradeTraceGraphNodeView(FlexibleModel):
    id: str


class TradeTraceGraphEdgeView(FlexibleModel):
    from_: str = Field(alias="from")
    to: str
    relation: str


class TradeTraceGraphView(FlexibleModel):
    nodes: list[TradeTraceGraphNodeView] = Field(default_factory=list)
    edges: list[TradeTraceGraphEdgeView] = Field(default_factory=list)


class TradeTraceListItemView(FlexibleModel):
    trace_id: str
    signal_id: Optional[str] = None
    symbol: Optional[str] = None
    timeframe: Optional[str] = None
    strategy: Optional[str] = None
    status: Optional[str] = None
    started_at: Optional[str] = None
    last_event_at: Optional[str] = None
    event_count: int = 0
    last_stage: Optional[str] = None
    reason: Optional[str] = None
    admission: Optional[TradeTraceAdmissionView] = None


class TradeTraceView(FlexibleModel):
    signal_id: Optional[str] = None
    trace_id: Optional[str] = None
    found: bool
    identifiers: TradeTraceIdentifiersView
    summary: TradeTraceSummaryView
    timeline: list[TradeTraceTimelineEventView] = Field(default_factory=list)
    graph: TradeTraceGraphView
    facts: dict[str, Any] = Field(default_factory=dict)
    related_signals: dict[str, Any] = Field(default_factory=dict)
    related_trade_audits: list[dict[str, Any]] = Field(default_factory=list)
    related_pipeline_events: list[dict[str, Any]] = Field(default_factory=list)
