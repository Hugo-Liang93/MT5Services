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
PIPELINE_EXECUTION_SUCCEEDED = "execution_succeeded"
PIPELINE_VOTING_COMPLETED = "voting_completed"
PIPELINE_EXECUTION_SKIPPED = "execution_skipped"
PIPELINE_ADMISSION_REPORT_APPENDED = "admission_report_appended"
PIPELINE_INTENT_PUBLISHED = "intent_published"
PIPELINE_INTENT_CLAIMED = "intent_claimed"
PIPELINE_INTENT_RECLAIMED = "intent_reclaimed"
PIPELINE_INTENT_DEAD_LETTERED = "intent_dead_lettered"
PIPELINE_COMMAND_SUBMITTED = "command_submitted"
PIPELINE_COMMAND_CLAIMED = "command_claimed"
PIPELINE_COMMAND_COMPLETED = "command_completed"
PIPELINE_COMMAND_FAILED = "command_failed"
PIPELINE_RISK_STATE_CHANGED = "risk_state_changed"
PIPELINE_UNMANAGED_POSITION_DETECTED = "unmanaged_position_detected"


@dataclass(frozen=True)
class PipelineStageDefinition:
    summary_key: str
    event_types: frozenset[str]


PIPELINE_STAGE_DEFINITIONS: tuple[PipelineStageDefinition, ...] = (
    PipelineStageDefinition("pipeline_bar_closed", frozenset({PIPELINE_BAR_CLOSED})),
    PipelineStageDefinition(
        "pipeline_indicator_computed",
        frozenset({PIPELINE_INDICATOR_COMPUTED}),
    ),
    PipelineStageDefinition(
        "pipeline_snapshot_published",
        frozenset({PIPELINE_SNAPSHOT_PUBLISHED}),
    ),
    PipelineStageDefinition(
        "pipeline_signal_filter",
        frozenset({PIPELINE_SIGNAL_FILTER_DECIDED}),
    ),
    PipelineStageDefinition(
        "pipeline_signal_evaluated",
        frozenset({PIPELINE_SIGNAL_EVALUATED}),
    ),
    PipelineStageDefinition(
        "pipeline_execution_decision",
        frozenset({PIPELINE_EXECUTION_DECIDED}),
    ),
    PipelineStageDefinition(
        "pipeline_execution_block",
        frozenset({PIPELINE_EXECUTION_BLOCKED}),
    ),
    PipelineStageDefinition(
        "pipeline_execution_submission",
        frozenset({PIPELINE_EXECUTION_SUBMITTED, PIPELINE_PENDING_ORDER_SUBMITTED}),
    ),
    PipelineStageDefinition(
        "pipeline_execution_failure",
        frozenset({PIPELINE_EXECUTION_FAILED}),
    ),
    PipelineStageDefinition(
        "pipeline_execution_success",
        frozenset({PIPELINE_EXECUTION_SUCCEEDED}),
    ),
    PipelineStageDefinition(
        "pipeline_voting",
        frozenset({PIPELINE_VOTING_COMPLETED}),
    ),
    PipelineStageDefinition(
        "pipeline_execution_skip",
        frozenset({PIPELINE_EXECUTION_SKIPPED}),
    ),
    PipelineStageDefinition(
        "pipeline_admission",
        frozenset({PIPELINE_ADMISSION_REPORT_APPENDED}),
    ),
    PipelineStageDefinition(
        "pipeline_intent",
        frozenset(
            {
                PIPELINE_INTENT_PUBLISHED,
                PIPELINE_INTENT_CLAIMED,
                PIPELINE_INTENT_RECLAIMED,
                PIPELINE_INTENT_DEAD_LETTERED,
            }
        ),
    ),
    PipelineStageDefinition(
        "pipeline_command",
        frozenset(
            {
                PIPELINE_COMMAND_SUBMITTED,
                PIPELINE_COMMAND_CLAIMED,
                PIPELINE_COMMAND_COMPLETED,
                PIPELINE_COMMAND_FAILED,
            }
        ),
    ),
    PipelineStageDefinition(
        "pipeline_risk_state",
        frozenset({PIPELINE_RISK_STATE_CHANGED}),
    ),
    PipelineStageDefinition(
        "pipeline_unmanaged_position",
        frozenset({PIPELINE_UNMANAGED_POSITION_DETECTED}),
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
    normalized_types = {str(row.get("event_type") or "").strip() for row in rows}
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
        PIPELINE_EXECUTION_SUCCEEDED: "pipeline.execution_succeeded",
        PIPELINE_VOTING_COMPLETED: "pipeline.voting_completed",
        PIPELINE_EXECUTION_SKIPPED: "pipeline.execution_skipped",
        PIPELINE_ADMISSION_REPORT_APPENDED: "pipeline.admission_report",
        PIPELINE_INTENT_PUBLISHED: "pipeline.intent_published",
        PIPELINE_INTENT_CLAIMED: "pipeline.intent_claimed",
        PIPELINE_INTENT_RECLAIMED: "pipeline.intent_reclaimed",
        PIPELINE_INTENT_DEAD_LETTERED: "pipeline.intent_dead_lettered",
        PIPELINE_COMMAND_SUBMITTED: "pipeline.command_submitted",
        PIPELINE_COMMAND_CLAIMED: "pipeline.command_claimed",
        PIPELINE_COMMAND_COMPLETED: "pipeline.command_completed",
        PIPELINE_COMMAND_FAILED: "pipeline.command_failed",
        PIPELINE_RISK_STATE_CHANGED: "pipeline.risk_state_changed",
        PIPELINE_UNMANAGED_POSITION_DETECTED: "pipeline.unmanaged_position_detected",
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
    if event_type == PIPELINE_EXECUTION_SUCCEEDED:
        return "completed"
    if event_type == PIPELINE_VOTING_COMPLETED:
        return str(payload.get("winning_direction") or "voted")
    if event_type == PIPELINE_EXECUTION_SKIPPED:
        return "skipped"
    if event_type == PIPELINE_ADMISSION_REPORT_APPENDED:
        return str(payload.get("decision") or "observed")
    if event_type == PIPELINE_INTENT_PUBLISHED:
        return "published"
    if event_type == PIPELINE_INTENT_CLAIMED:
        return "claimed"
    if event_type == PIPELINE_INTENT_RECLAIMED:
        return "reclaimed"
    if event_type == PIPELINE_INTENT_DEAD_LETTERED:
        return "dead_lettered"
    if event_type == PIPELINE_COMMAND_SUBMITTED:
        return "submitted"
    if event_type == PIPELINE_COMMAND_CLAIMED:
        return "claimed"
    if event_type == PIPELINE_COMMAND_COMPLETED:
        return "completed"
    if event_type == PIPELINE_COMMAND_FAILED:
        return "failed"
    if event_type == PIPELINE_RISK_STATE_CHANGED:
        return "updated"
    if event_type == PIPELINE_UNMANAGED_POSITION_DETECTED:
        return "detected"
    return "observed"


def pipeline_event_summary(row: Mapping[str, Any]) -> str:
    event_type = str(row.get("event_type") or "")
    payload = row.get("payload") or {}
    if event_type == PIPELINE_BAR_CLOSED:
        return "bar closed"
    if event_type == PIPELINE_INDICATOR_COMPUTED:
        return "indicator computed"
    if event_type == PIPELINE_SNAPSHOT_PUBLISHED:
        return "snapshot published"
    if event_type == PIPELINE_SIGNAL_FILTER_DECIDED:
        return (
            f"signal filter passed: {payload.get('category') or 'pass'}"
            if payload.get("allowed")
            else f"signal filter blocked: {payload.get('reason') or 'unknown'}"
        )
    if event_type == PIPELINE_SIGNAL_EVALUATED:
        strategy = payload.get("strategy") or "unknown"
        direction = payload.get("direction") or "hold"
        return f"signal evaluated: {strategy}/{direction}"
    if event_type == PIPELINE_EXECUTION_DECIDED:
        strategy = payload.get("strategy") or "unknown"
        direction = payload.get("direction") or "hold"
        return f"execution ready: {strategy}/{direction}"
    if event_type == PIPELINE_EXECUTION_BLOCKED:
        return f"execution blocked: {payload.get('reason') or 'unknown'}"
    if event_type == PIPELINE_EXECUTION_SUBMITTED:
        order_kind = payload.get("order_kind") or "market"
        direction = payload.get("direction") or "hold"
        return f"market order submitted: {direction}/{order_kind}"
    if event_type == PIPELINE_PENDING_ORDER_SUBMITTED:
        order_kind = payload.get("order_kind") or "pending"
        direction = payload.get("direction") or "hold"
        return f"pending order submitted: {direction}/{order_kind}"
    if event_type == PIPELINE_EXECUTION_FAILED:
        return f"execution failed: {payload.get('reason') or payload.get('error') or 'unknown'}"
    if event_type == PIPELINE_EXECUTION_SUCCEEDED:
        return "execution completed"
    if event_type == PIPELINE_VOTING_COMPLETED:
        group = payload.get("group_name") or "consensus"
        direction = payload.get("winning_direction") or "none"
        conf = payload.get("final_confidence")
        conf_str = f" conf={conf:.3f}" if conf is not None else ""
        return f"voting completed: {group}/{direction}{conf_str}"
    if event_type == PIPELINE_EXECUTION_SKIPPED:
        reason = payload.get("skip_reason") or "unknown"
        strategy = payload.get("strategy") or "?"
        return f"execution skipped: {strategy} ({reason})"
    if event_type == PIPELINE_ADMISSION_REPORT_APPENDED:
        return f"admission decision: {payload.get('decision') or 'unknown'}"
    if event_type == PIPELINE_INTENT_PUBLISHED:
        return "intent published"
    if event_type == PIPELINE_INTENT_CLAIMED:
        return "intent claimed"
    if event_type == PIPELINE_INTENT_RECLAIMED:
        return "intent reclaimed"
    if event_type == PIPELINE_INTENT_DEAD_LETTERED:
        return "intent dead-lettered"
    if event_type == PIPELINE_COMMAND_SUBMITTED:
        return "command submitted"
    if event_type == PIPELINE_COMMAND_CLAIMED:
        return "command claimed"
    if event_type == PIPELINE_COMMAND_COMPLETED:
        return "command completed"
    if event_type == PIPELINE_COMMAND_FAILED:
        return f"command failed: {payload.get('error') or payload.get('reason') or 'unknown'}"
    if event_type == PIPELINE_RISK_STATE_CHANGED:
        return "risk state changed"
    if event_type == PIPELINE_UNMANAGED_POSITION_DETECTED:
        return "unmanaged live position detected"
    return f"pipeline event: {event_type}"
