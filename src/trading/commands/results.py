from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

_CORE_FIELDS = frozenset(
    {
        "accepted",
        "status",
        "action_id",
        "command_id",
        "audit_id",
        "actor",
        "reason",
        "idempotency_key",
        "request_context",
        "message",
        "error_code",
        "recorded_at",
        "effective_state",
        "error_message",
        "details",
        "replayed",
    }
)


def _string_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalize_request_context(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _normalize_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _normalize_recorded_at(value: Any) -> str:
    if isinstance(value, datetime):
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return normalized.isoformat()
    text = str(value or "").strip()
    if text:
        return text
    return datetime.now(timezone.utc).isoformat()


def _extra_fields_from_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in dict(value).items() if key not in _CORE_FIELDS}


def build_operator_command_result(
    *,
    accepted: bool,
    status: str,
    action_id: str | None,
    command_id: str | None = None,
    audit_id: str | None = None,
    actor: str | None = None,
    reason: str | None = None,
    idempotency_key: str | None = None,
    request_context: Mapping[str, Any] | None = None,
    message: str | None = None,
    error_code: str | None = None,
    recorded_at: Any = None,
    effective_state: Mapping[str, Any] | None = None,
    error_message: str | None = None,
    details: Mapping[str, Any] | None = None,
    replayed: bool = False,
    extra_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "accepted": bool(accepted),
        "status": str(status or "").strip() or "failed",
        "action_id": _string_or_none(action_id),
        "command_id": _string_or_none(command_id),
        "audit_id": _string_or_none(audit_id),
        "actor": _string_or_none(actor),
        "reason": _string_or_none(reason),
        "idempotency_key": _string_or_none(idempotency_key),
        "request_context": _normalize_request_context(request_context),
        "message": _string_or_none(message),
        "error_code": _string_or_none(error_code),
        "recorded_at": _normalize_recorded_at(recorded_at),
        "effective_state": _normalize_mapping(effective_state),
    }
    if error_message is not None:
        payload["error_message"] = str(error_message)
    if details:
        payload["details"] = _normalize_mapping(details)
    if replayed:
        payload["replayed"] = True
    if extra_fields:
        payload.update(_extra_fields_from_mapping(extra_fields))
    return payload


def bind_command_item_result(
    *,
    item: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    raw = dict(result)
    status = str(raw.get("status") or "").strip()
    if not status:
        raise ValueError("operator_command_result_missing_status")
    message = _string_or_none(raw.get("message"))
    if message is None:
        raise ValueError("operator_command_result_missing_message")
    action_id = _string_or_none(raw.get("action_id")) or _string_or_none(
        item.get("action_id")
    ) or _string_or_none(item.get("command_id"))
    if action_id is None:
        raise ValueError("operator_command_result_missing_action_id")
    return build_operator_command_result(
        accepted=bool(raw.get("accepted")) if "accepted" in raw else status != "failed",
        status=status,
        action_id=action_id,
        command_id=_string_or_none(item.get("command_id")),
        audit_id=_string_or_none(raw.get("audit_id")) or _string_or_none(item.get("audit_id")),
        actor=_string_or_none(raw.get("actor")) or _string_or_none(item.get("actor")),
        reason=_string_or_none(raw.get("reason")) or _string_or_none(item.get("reason")),
        idempotency_key=_string_or_none(raw.get("idempotency_key"))
        or _string_or_none(item.get("idempotency_key")),
        request_context=raw.get("request_context")
        if isinstance(raw.get("request_context"), Mapping)
        else item.get("request_context"),
        message=message,
        error_code=_string_or_none(raw.get("error_code")),
        recorded_at=raw.get("recorded_at"),
        effective_state=_normalize_mapping(raw.get("effective_state")),
        error_message=_string_or_none(raw.get("error_message")),
        details=_normalize_mapping(raw.get("details")),
        replayed=bool(raw.get("replayed")),
        extra_fields=raw,
    )


def build_existing_command_result(row: Mapping[str, Any]) -> dict[str, Any]:
    response_payload = _normalize_mapping(row.get("response_payload"))
    status = (
        _string_or_none(response_payload.get("status"))
        or _string_or_none(row.get("status"))
        or "pending"
    )
    effective_state = _normalize_mapping(response_payload.get("effective_state"))
    if not effective_state:
        effective_state = {
            "command_type": row.get("command_type"),
            "target_account_alias": row.get("target_account_alias"),
            "target_account_key": row.get("target_account_key"),
        }
    message = _string_or_none(response_payload.get("message"))
    if message is None:
        message = (
            "operator command already queued"
            if status == "pending"
            else f"operator command {status}"
        )
    action_id = _string_or_none(response_payload.get("action_id")) or _string_or_none(
        row.get("action_id")
    ) or _string_or_none(row.get("command_id"))
    return build_operator_command_result(
        accepted=bool(response_payload.get("accepted"))
        if "accepted" in response_payload
        else status != "failed",
        status=status,
        action_id=action_id,
        command_id=_string_or_none(row.get("command_id")),
        audit_id=_string_or_none(response_payload.get("audit_id"))
        or _string_or_none(row.get("audit_id")),
        actor=_string_or_none(response_payload.get("actor")) or _string_or_none(row.get("actor")),
        reason=_string_or_none(response_payload.get("reason"))
        or _string_or_none(row.get("reason")),
        idempotency_key=_string_or_none(response_payload.get("idempotency_key"))
        or _string_or_none(row.get("idempotency_key")),
        request_context=response_payload.get("request_context")
        if isinstance(response_payload.get("request_context"), Mapping)
        else row.get("request_context"),
        message=message,
        error_code=_string_or_none(response_payload.get("error_code"))
        or _string_or_none(row.get("last_error_code")),
        recorded_at=response_payload.get("recorded_at") or row.get("created_at"),
        effective_state=effective_state,
        error_message=_string_or_none(response_payload.get("error_message")),
        details=_normalize_mapping(response_payload.get("details")),
        replayed=True,
        extra_fields=response_payload,
    )
