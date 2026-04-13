from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import ApiResponse


def normalize_action_actor(actor: Optional[str]) -> str:
    if not isinstance(actor, str):
        return "operator"
    normalized = actor.strip()
    return normalized or "operator"


def normalize_idempotency_key(idempotency_key: Optional[str]) -> Optional[str]:
    if not isinstance(idempotency_key, str):
        return None
    normalized = idempotency_key.strip()
    return normalized or None


def normalize_request_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def next_action_id() -> str:
    return uuid4().hex


def build_action_result(
    *,
    action_id: str,
    audit_id: Optional[str],
    actor: str,
    reason: Optional[str],
    idempotency_key: Optional[str],
    request_context: dict[str, Any],
    status: str,
    message: str,
    recorded_at: Optional[str],
    effective_state: dict[str, Any],
    extra_fields: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload = {
        "accepted": True,
        "status": status,
        "action_id": action_id,
        "audit_id": audit_id,
        "actor": actor,
        "reason": reason,
        "idempotency_key": idempotency_key,
        "request_context": dict(request_context),
        "message": message,
        "error_code": None,
        "recorded_at": recorded_at,
        "effective_state": dict(effective_state),
    }
    if extra_fields:
        payload.update(extra_fields)
    return payload


def build_action_error_details(
    *,
    action_id: str,
    audit_id: Optional[str],
    actor: str,
    reason: Optional[str],
    idempotency_key: Optional[str],
    request_context: dict[str, Any],
    recorded_at: Optional[str],
    operation: str,
    extra_details: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    details = {
        "operation": operation,
        "accepted": False,
        "status": "failed",
        "action_id": action_id,
        "audit_id": audit_id,
        "actor": actor,
        "reason": reason,
        "idempotency_key": idempotency_key,
        "request_context": dict(request_context),
        "recorded_at": recorded_at,
    }
    if extra_details:
        details.update(extra_details)
    return details


def build_action_error_payload(
    *,
    action_id: str,
    audit_id: Optional[str],
    actor: str,
    reason: Optional[str],
    idempotency_key: Optional[str],
    request_context: dict[str, Any],
    error_code: Any,
    error_message: str,
    suggested_action: Any,
    operation: str,
    extra_details: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "accepted": False,
        "status": "failed",
        "action_id": action_id,
        "audit_id": audit_id,
        "actor": actor,
        "reason": reason,
        "idempotency_key": idempotency_key,
        "request_context": dict(request_context),
        "message": error_message,
        "error_code": getattr(error_code, "value", error_code),
        "error_message": error_message,
        "suggested_action": getattr(suggested_action, "value", suggested_action),
        "details": build_action_error_details(
            action_id=action_id,
            audit_id=audit_id,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
            request_context=request_context,
            recorded_at=None,
            operation=operation,
            extra_details=extra_details,
        ),
        "recorded_at": None,
    }


def build_idempotency_conflict_response(
    *,
    operation: str,
    command_type: str,
    idempotency_key: str,
    existing_record: Optional[dict[str, Any]] = None,
    extra_metadata: Optional[dict[str, Any]] = None,
) -> ApiResponse[None]:
    existing_record = dict(existing_record or {})
    metadata = {
        "timestamp": datetime.now().isoformat(),
        "data_source": "error",
        "operation": operation,
        "command_type": command_type,
        "idempotency_key": idempotency_key,
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return ApiResponse(
        success=False,
        data=None,
        error={
            "code": str(AIErrorCode.INVALID_REQUEST.value).lower(),
            "message": (
                f"idempotency_key '{idempotency_key}' already used for a different "
                f"{command_type} request"
            ),
            "suggested_action": AIErrorAction.REVIEW_PARAMETERS.value,
            "details": {
                "operation": operation,
                "command_type": command_type,
                "idempotency_key": idempotency_key,
                "existing_action_id": existing_record.get("action_id"),
                "existing_audit_id": existing_record.get("audit_id"),
                "existing_recorded_at": existing_record.get("recorded_at"),
                "existing_request_payload": dict(
                    existing_record.get("request_payload") or {}
                ),
                "existing_response_payload": dict(
                    existing_record.get("response_payload") or {}
                ),
            },
        },
        metadata=metadata,
    )


def build_replayed_action_response(
    *,
    operation: str,
    replayed: dict[str, Any],
    extra_metadata: Optional[dict[str, Any]] = None,
    default_error_code: Any = None,
    default_suggested_action: Any = None,
) -> ApiResponse[Any]:
    response_payload = dict((replayed or {}).get("response_payload") or {})
    metadata = {
        "operation": operation,
        "replayed": True,
        "replay_source": str((replayed or {}).get("source") or "memory"),
        "action_id": response_payload.get("action_id"),
        "audit_id": response_payload.get("audit_id"),
        "actor": response_payload.get("actor"),
        "idempotency_key": response_payload.get("idempotency_key"),
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    if response_payload.get("accepted") is False:
        error_code = (
            response_payload.get("error_code")
            or default_error_code
            or AIErrorCode.INVALID_REQUEST
        )
        suggested_action = (
            response_payload.get("suggested_action")
            or default_suggested_action
            or AIErrorAction.REVIEW_PARAMETERS
        )
        return ApiResponse(
            success=False,
            data=None,
            error={
                "code": str(getattr(error_code, "value", error_code)).lower(),
                "message": str(
                    response_payload.get("error_message")
                    or response_payload.get("message")
                    or "replayed operator action failed"
                ),
                "suggested_action": getattr(suggested_action, "value", suggested_action),
                "details": dict(response_payload.get("details") or {}),
            },
            metadata={
                "timestamp": datetime.now().isoformat(),
                "data_source": "error",
                **metadata,
            },
        )
    return ApiResponse.success_response(data=response_payload, metadata=metadata)
