"""Recovery runner module-level helpers (N1: 从 runner.py 拆出)。

无状态工具函数集合：cycle id 工厂 / decision payload 序列化 / submitted ticket
管理 / close result 判定 / position matching / 时间格式化。本文件只持有纯函数，
runner.py 的主类仅作为 import 消费方。

拆分目的：将 runner.py 从 1928 → ~1300 行，让主类（DemoBoundedRecoveryRunner）
专注生命周期与决策编排，helper 通过显式 import 接入便于测试与复用。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Mapping

from .models import RecoveryCycleState


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_cycle_id(now: datetime) -> str:
    millis = int(now.timestamp() * 1000)
    return f"demo-recovery-runner-{millis}"


def default_source_signal_id(cycle_id: str) -> str:
    return f"{cycle_id}-signal"


def cycle_close_status_reason(reason: str | None, *, dry_run: bool) -> str:
    suffix = "dry_run" if dry_run else "submitted"
    reason_key = str(reason or "").strip().lower()
    mapping = {
        "net_recovery_target_reached": "resident_recovery_target_reached",
        "cycle_loss_limit_reached": "resident_recovery_cycle_loss_limit",
        "max_cycle_duration_reached": "resident_recovery_max_cycle_duration",
        "max_steps_exit_reached": "resident_recovery_max_steps_exit",
        "max_steps_hold_time_reached": "resident_recovery_max_steps_hold_time",
    }
    prefix = mapping.get(reason_key, "resident_recovery_cycle_closed")
    return f"{prefix}_{suffix}"


def decision_counts_payload(counter: Counter[str]) -> dict[str, int]:
    keys = ("open_initial", "open_step", "close_cycle", "hold", "block", "ignored")
    payload = {key: int(counter.get(key, 0)) for key in keys}
    for key, value in counter.items():
        payload.setdefault(str(key), int(value))
    return payload


def decision_payload(decision: Any) -> dict[str, Any]:
    return {
        "action": getattr(decision, "action", None),
        "reason": getattr(decision, "reason", None),
        "step_index": getattr(decision, "step_index", None),
        "volume": getattr(decision, "volume", None),
        "entry_price": getattr(decision, "entry_price", None),
        "exit_price": getattr(decision, "exit_price", None),
        "metadata": dict(getattr(decision, "metadata", {}) or {}),
    }


def policy_decision_payload(decision: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "reason": getattr(decision, "reason", None),
        "metadata": dict(getattr(decision, "metadata", {}) or {}),
    }
    if hasattr(decision, "direction"):
        payload["direction"] = getattr(decision, "direction")
    if hasattr(decision, "allowed"):
        payload["allowed"] = bool(getattr(decision, "allowed"))
    return payload


def cycle_with_submitted_ticket(
    cycle: RecoveryCycleState,
    *,
    scope: str,
    step_index: int,
    execution_result: dict[str, Any],
) -> RecoveryCycleState:
    ticket = ticket_from_nested_payload(execution_result)
    if ticket is None:
        return cycle
    metadata = dict(cycle.metadata or {})
    items = [
        dict(item)
        for item in list(metadata.get("submitted_tickets") or [])
        if isinstance(item, dict)
    ]
    normalized = {
        "scope": str(scope),
        "step_index": int(step_index),
        "ticket": int(ticket),
    }
    existing = {
        (str(item.get("scope")), int(item.get("ticket") or 0)) for item in items
    }
    if (normalized["scope"], normalized["ticket"]) not in existing:
        items.append(normalized)
    metadata["submitted_tickets"] = items
    return replace(cycle, metadata=metadata)


def submitted_tickets(cycle: RecoveryCycleState) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in list((cycle.metadata or {}).get("submitted_tickets") or []):
        if not isinstance(item, dict):
            continue
        try:
            ticket = int(item.get("ticket"))
        except (TypeError, ValueError):
            continue
        if ticket <= 0 or ticket in seen:
            continue
        seen.add(ticket)
        items.append(
            {
                "scope": str(item.get("scope") or f"ticket_{ticket}"),
                "step_index": int(item.get("step_index") or 0),
                "ticket": ticket,
            }
        )
    return items


def ticket_from_nested_payload(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in ("ticket", "position", "order", "order_id", "deal", "deal_id"):
        try:
            ticket = int(payload.get(key))
        except (TypeError, ValueError):
            continue
        if ticket > 0:
            return ticket
    for key in ("result", "response", "response_payload"):
        ticket = ticket_from_nested_payload(payload.get(key))
        if ticket is not None:
            return ticket
    return None


def close_result_success(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("success") is True:
        return True
    status = str(result.get("status") or result.get("retcode") or "").strip().lower()
    if status in {"ok", "closed", "success", "done", "completed", "10009"}:
        if "accepted" not in result or bool(result.get("accepted")):
            return True
    effective_state = result.get("effective_state")
    if isinstance(effective_state, dict):
        nested = effective_state.get("result")
        if isinstance(nested, dict) and nested.get("success") is True:
            return True
    nested_result = result.get("result")
    if isinstance(nested_result, dict) and nested_result.get("success") is True:
        return True
    return False


def close_result_position_absent(result: Any) -> bool:
    if not isinstance(result, Mapping):
        return False
    text_fields: list[str] = []
    for key in ("message", "error_message", "reason", "error_code", "status"):
        value = result.get(key)
        if value is not None:
            text_fields.append(str(value))
    details = result.get("details")
    if isinstance(details, Mapping):
        for value in details.values():
            if value is not None:
                text_fields.append(str(value))
    effective_state = result.get("effective_state")
    if isinstance(effective_state, Mapping):
        for value in effective_state.values():
            if value is not None:
                text_fields.append(str(value))
    return position_absent_message(" ".join(text_fields))


def position_absent_message(message: str) -> bool:
    normalized = str(message or "").strip().lower().replace("_", " ")
    if not normalized:
        return False
    return "position" in normalized and (
        "not found" in normalized
        or "already closed" in normalized
        or "missing" in normalized
    )


def cleanup_result_metadata(close_result: Mapping[str, Any]) -> dict[str, Any]:
    results = [
        dict(item)
        for item in list(close_result.get("results") or [])
        if isinstance(item, Mapping)
    ]
    tickets_by_status: dict[str, list[int]] = {
        "closed": [],
        "already_closed": [],
        "failed": [],
    }
    for item in results:
        status = str(item.get("status") or "unknown")
        try:
            ticket = int(item.get("ticket"))
        except (TypeError, ValueError):
            continue
        if ticket <= 0:
            continue
        tickets_by_status.setdefault(status, []).append(ticket)
    return {
        "status": close_result.get("status"),
        "reason": close_result.get("reason"),
        "tickets": unique_positive_ints(close_result.get("tickets")),
        "submitted_tickets": unique_positive_ints(
            close_result.get("submitted_tickets")
        ),
        "closed_tickets": tickets_by_status.get("closed", []),
        "already_closed_tickets": tickets_by_status.get("already_closed", []),
        "failed_tickets": tickets_by_status.get("failed", []),
    }


def matching_recovery_positions(
    cycle: RecoveryCycleState,
    positions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    submitted = {int(item["ticket"]) for item in submitted_tickets(cycle)}
    matches: list[dict[str, Any]] = []
    for position in positions:
        account_key = str(position.get("account_key") or "").strip()
        if account_key and account_key != cycle.account_key:
            continue
        ticket = position_ticket(position)
        if ticket is not None and ticket in submitted:
            matches.append(position)
            continue
        if position_identity_matches_cycle(cycle, position):
            matches.append(position)
    return matches


def position_identity_matches_cycle(
    cycle: RecoveryCycleState,
    position: Mapping[str, Any],
) -> bool:
    cycle_id = str(cycle.cycle_id or "").strip()
    source_signal_id = str(cycle.source_signal_id or "").strip()
    metadata = position.get("metadata")
    if isinstance(metadata, Mapping):
        metadata_cycle_id = str(metadata.get("recovery_cycle_id") or "").strip()
        metadata_source_signal_id = str(metadata.get("source_signal_id") or "").strip()
        if cycle_id and metadata_cycle_id == cycle_id:
            return True
        if source_signal_id and metadata_source_signal_id == source_signal_id:
            return True

    identity_fields = (
        "signal_id",
        "source_signal_id",
        "trace_id",
        "request_id",
        "action_id",
        "comment",
    )
    for key in identity_fields:
        value = str(position.get(key) or "").strip()
        if not value:
            continue
        if source_signal_id and value == source_signal_id:
            return True
        if cycle_id and cycle_id in value:
            return True
    return False


def position_match_payload(position: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ticket": position_ticket(position),
        "signal_id": str(position.get("signal_id") or ""),
        "comment": str(position.get("comment") or ""),
        "symbol": str(position.get("symbol") or ""),
        "strategy": str(position.get("strategy") or ""),
        "timeframe": str(position.get("timeframe") or ""),
    }


def unique_positive_ints(values: Any) -> list[int]:
    items: list[int] = []
    seen: set[int] = set()
    for value in list(values or []):
        try:
            item = int(value)
        except (TypeError, ValueError):
            continue
        if item <= 0 or item in seen:
            continue
        seen.add(item)
        items.append(item)
    return items


def position_ticket(position: Mapping[str, Any]) -> int | None:
    for key in ("ticket", "position_ticket", "position", "order_ticket"):
        try:
            ticket = int(position.get(key))
        except (TypeError, ValueError):
            continue
        if ticket > 0:
            return ticket
    return None


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def datetime_after_or_equal(left: datetime, right: datetime) -> bool:
    left_aware = left if left.tzinfo is not None else left.replace(tzinfo=timezone.utc)
    right_aware = (
        right if right.tzinfo is not None else right.replace(tzinfo=timezone.utc)
    )
    return left_aware >= right_aware


def iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()
