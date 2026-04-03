from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


PIPELINE_BAR_CLOSED = "bar_closed"
PIPELINE_INDICATOR_COMPUTED = "indicator_computed"
PIPELINE_SNAPSHOT_PUBLISHED = "snapshot_published"
PIPELINE_SIGNAL_FILTER_DECIDED = "signal_filter_decided"
PIPELINE_SIGNAL_EVALUATED = "signal_evaluated"
PIPELINE_EXECUTION_DECIDED = "execution_decided"
PIPELINE_EXECUTION_BLOCKED = "execution_blocked"
PIPELINE_EXECUTION_SUBMITTED = "execution_submitted"
PIPELINE_PENDING_ORDER_SUBMITTED = "pending_order_submitted"
PIPELINE_EXECUTION_FAILED = "execution_failed"


@dataclass(frozen=True)
class PipelineStageDefinition:
    summary_key: str
    event_types: frozenset[str]


PIPELINE_STAGE_DEFINITIONS: tuple[PipelineStageDefinition, ...] = (
    PipelineStageDefinition("pipeline_bar_closed", frozenset({PIPELINE_BAR_CLOSED})),
    PipelineStageDefinition(
        "pipeline_indicator_computed", frozenset({PIPELINE_INDICATOR_COMPUTED})
    ),
    PipelineStageDefinition(
        "pipeline_snapshot_published", frozenset({PIPELINE_SNAPSHOT_PUBLISHED})
    ),
    PipelineStageDefinition(
        "pipeline_signal_filter", frozenset({PIPELINE_SIGNAL_FILTER_DECIDED})
    ),
    PipelineStageDefinition(
        "pipeline_signal_evaluated", frozenset({PIPELINE_SIGNAL_EVALUATED})
    ),
    PipelineStageDefinition(
        "pipeline_execution_decision", frozenset({PIPELINE_EXECUTION_DECIDED})
    ),
    PipelineStageDefinition(
        "pipeline_execution_block", frozenset({PIPELINE_EXECUTION_BLOCKED})
    ),
    PipelineStageDefinition(
        "pipeline_execution_submission",
        frozenset({PIPELINE_EXECUTION_SUBMITTED, PIPELINE_PENDING_ORDER_SUBMITTED}),
    ),
    PipelineStageDefinition(
        "pipeline_execution_failure", frozenset({PIPELINE_EXECUTION_FAILED})
    ),
)


def pipeline_stage_presence(
    rows: Iterable[Mapping[str, Any]],
    summary_key: str,
) -> bool:
    definition = next(
        (item for item in PIPELINE_STAGE_DEFINITIONS if item.summary_key == summary_key),
        None,
    )
    if definition is None:
        return False
    normalized_types = {
        str(row.get("event_type") or "").strip()
        for row in rows
    }
    return bool(normalized_types.intersection(definition.event_types))


def pipeline_stage_name(event_type: str) -> str:
    normalized = str(event_type or "unknown").strip()
    mapping = {
        PIPELINE_BAR_CLOSED: "pipeline.bar_closed",
        PIPELINE_INDICATOR_COMPUTED: "pipeline.indicator_computed",
        PIPELINE_SNAPSHOT_PUBLISHED: "pipeline.snapshot_published",
        PIPELINE_SIGNAL_FILTER_DECIDED: "pipeline.signal_filter",
        PIPELINE_SIGNAL_EVALUATED: "pipeline.signal_evaluated",
        PIPELINE_EXECUTION_DECIDED: "pipeline.execution_decision",
        PIPELINE_EXECUTION_BLOCKED: "pipeline.execution_blocked",
        PIPELINE_EXECUTION_SUBMITTED: "pipeline.execution_submitted",
        PIPELINE_PENDING_ORDER_SUBMITTED: "pipeline.pending_order_submitted",
        PIPELINE_EXECUTION_FAILED: "pipeline.execution_failed",
    }
    return mapping.get(normalized, f"pipeline.{normalized}")


def pipeline_event_status(row: Mapping[str, Any]) -> str:
    event_type = str(row.get("event_type") or "")
    payload = row.get("payload") or {}
    if event_type == PIPELINE_SIGNAL_FILTER_DECIDED:
        return "passed" if payload.get("allowed") else "blocked"
    if event_type == PIPELINE_SIGNAL_EVALUATED:
        return str(payload.get("signal_state") or "evaluated")
    if event_type == PIPELINE_EXECUTION_DECIDED:
        return "ready"
    if event_type == PIPELINE_EXECUTION_BLOCKED:
        return "blocked"
    if event_type in {PIPELINE_EXECUTION_SUBMITTED, PIPELINE_PENDING_ORDER_SUBMITTED}:
        return "submitted"
    if event_type == PIPELINE_EXECUTION_FAILED:
        return "failed"
    return "observed"


def pipeline_event_summary(row: Mapping[str, Any]) -> str:
    event_type = str(row.get("event_type") or "")
    payload = row.get("payload") or {}
    if event_type == PIPELINE_BAR_CLOSED:
        return "行情 bar close"
    if event_type == PIPELINE_INDICATOR_COMPUTED:
        return "指标计算完成"
    if event_type == PIPELINE_SNAPSHOT_PUBLISHED:
        return "指标快照发布"
    if event_type == PIPELINE_SIGNAL_FILTER_DECIDED:
        return (
            f"信号过滤通过: {payload.get('category') or 'pass'}"
            if payload.get("allowed")
            else f"信号过滤拦截: {payload.get('reason') or 'unknown'}"
        )
    if event_type == PIPELINE_SIGNAL_EVALUATED:
        strategy = payload.get("strategy") or "unknown"
        direction = payload.get("direction") or "hold"
        return f"策略评估: {strategy}/{direction}"
    if event_type == PIPELINE_EXECUTION_DECIDED:
        strategy = payload.get("strategy") or "unknown"
        direction = payload.get("direction") or "hold"
        return f"执行层放行: {strategy}/{direction}"
    if event_type == PIPELINE_EXECUTION_BLOCKED:
        return f"执行层拦截: {payload.get('reason') or 'unknown'}"
    if event_type == PIPELINE_EXECUTION_SUBMITTED:
        order_kind = payload.get("order_kind") or "market"
        direction = payload.get("direction") or "hold"
        return f"市价执行已提交: {direction}/{order_kind}"
    if event_type == PIPELINE_PENDING_ORDER_SUBMITTED:
        order_kind = payload.get("order_kind") or "pending"
        direction = payload.get("direction") or "hold"
        return f"挂单已提交: {direction}/{order_kind}"
    if event_type == PIPELINE_EXECUTION_FAILED:
        return f"执行失败: {payload.get('reason') or 'unknown'}"
    return f"Pipeline 事件: {event_type}"
