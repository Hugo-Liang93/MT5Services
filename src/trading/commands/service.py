from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from src.config.mt5 import MT5Settings, load_group_mt5_settings
from src.config.runtime_identity import RuntimeIdentity, build_account_key
from src.monitoring.pipeline.event_bus import PipelineEvent, PipelineEventBus
from src.trading.application.idempotency import TradeOperatorActionReplayConflictError
from src.trading.commands.results import build_existing_command_result
from src.trading.commands.results import build_operator_command_result


class OperatorCommandService:
    def __init__(
        self,
        *,
        write_fn,
        fetch_fn,
        runtime_identity: RuntimeIdentity,
        pipeline_event_bus: PipelineEventBus | None = None,
    ) -> None:
        self._write_fn = write_fn
        self._fetch_fn = fetch_fn
        self._runtime_identity = runtime_identity
        self._pipeline_event_bus = pipeline_event_bus
        self._accounts = load_group_mt5_settings(
            instance_name=runtime_identity.instance_name,
        )

    def enqueue(
        self,
        *,
        command_type: str,
        payload: dict[str, Any] | None,
        actor: str | None,
        reason: str | None,
        action_id: str | None,
        idempotency_key: str | None,
        request_context: dict[str, Any] | None,
        target_account_alias: str | None = None,
    ) -> dict[str, Any]:
        resolved_account = self._resolve_target_account(target_account_alias)
        target_account_key = build_account_key(
            self._runtime_identity.environment,
            resolved_account.mt5_server,
            resolved_account.mt5_login,
        )
        normalized_command_type = str(command_type or "").strip()
        normalized_payload = dict(payload or {})
        normalized_actor = str(actor or "").strip() or "operator"
        normalized_reason = str(reason or "").strip() or None
        normalized_action_id = str(action_id or "").strip() or uuid4().hex
        normalized_idempotency_key = str(idempotency_key or "").strip() or None
        normalized_request_context = (
            dict(request_context) if isinstance(request_context, dict) else {}
        )
        existing = self._find_existing(
            command_type=normalized_command_type,
            target_account_key=target_account_key,
            idempotency_key=normalized_idempotency_key,
        )
        if existing is not None:
            normalized_request = self._normalized_request_contract(
                payload=normalized_payload,
                actor=normalized_actor,
                reason=normalized_reason,
                request_context=normalized_request_context,
            )
            existing_request = self._normalized_request_contract(
                payload=dict(existing.get("payload") or {}),
                actor=str(existing.get("actor") or "").strip() or "operator",
                reason=str(existing.get("reason") or "").strip() or None,
                request_context=dict(existing.get("request_context") or {}),
            )
            if existing_request != normalized_request:
                response_payload = dict(existing.get("response_payload") or {})
                raise TradeOperatorActionReplayConflictError(
                    (
                        f"idempotency_key '{normalized_idempotency_key}' already used for "
                        f"a different {normalized_command_type} request"
                    ),
                    command_type=normalized_command_type,
                    idempotency_key=normalized_idempotency_key,
                    existing_record={
                        "action_id": response_payload.get("action_id") or existing.get("action_id"),
                        "audit_id": response_payload.get("audit_id") or existing.get("audit_id"),
                        "recorded_at": response_payload.get("recorded_at")
                        or (
                            existing.get("created_at").isoformat()
                            if existing.get("created_at") is not None
                            else None
                        ),
                        "request_payload": existing_request,
                        "response_payload": response_payload,
                    },
                )
            return self._build_existing_response(existing)

        command_id = uuid4().hex
        created_at = datetime.now(timezone.utc)
        row = (
            created_at,
            command_id,
            normalized_command_type,
            target_account_key,
            resolved_account.account_alias,
            "pending",
            normalized_action_id,
            normalized_actor,
            normalized_reason,
            normalized_idempotency_key,
            normalized_request_context,
            normalized_payload,
            None,
            None,
            None,
            None,
            None,
            0,
            None,
            None,
            {},
            None,
        )
        self._write_fn([row])
        self._emit_command_submitted(
            command_id=command_id,
            command_type=normalized_command_type,
            action_id=normalized_action_id,
            payload=normalized_payload,
            target_account_key=target_account_key,
            target_account_alias=resolved_account.account_alias,
            recorded_at=created_at,
        )
        return build_operator_command_result(
            accepted=True,
            status="pending",
            action_id=normalized_action_id,
            command_id=command_id,
            audit_id=None,
            actor=normalized_actor,
            reason=normalized_reason,
            idempotency_key=normalized_idempotency_key,
            request_context=normalized_request_context,
            message="operator command accepted",
            error_code=None,
            recorded_at=created_at,
            effective_state={
                "command_type": normalized_command_type,
                "target_account_alias": resolved_account.account_alias,
                "target_account_key": target_account_key,
            },
        )

    def _find_existing(
        self,
        *,
        command_type: str,
        target_account_key: str,
        idempotency_key: str | None,
    ) -> dict[str, Any] | None:
        if idempotency_key is None:
            return None
        rows = self._fetch_fn(
            command_type=command_type,
            target_account_key=target_account_key,
            idempotency_key=idempotency_key,
            limit=1,
        )
        return dict(rows[0]) if rows else None

    def _resolve_target_account(self, target_account_alias: str | None) -> MT5Settings:
        alias = str(target_account_alias or "").strip() or self._runtime_identity.account_alias
        account = self._accounts.get(alias)
        if account is None:
            raise ValueError(f"unknown operator command target account: {alias}")
        return account

    @staticmethod
    def _normalized_request_contract(
        *,
        payload: dict[str, Any],
        actor: str,
        reason: str | None,
        request_context: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "payload": dict(payload or {}),
            "actor": actor,
            "reason": reason,
            "request_context": dict(request_context or {}),
        }

    @staticmethod
    def _build_existing_response(row: dict[str, Any]) -> dict[str, Any]:
        return build_existing_command_result(row)

    def _emit_command_submitted(
        self,
        *,
        command_id: str,
        command_type: str,
        action_id: str,
        payload: dict[str, Any],
        target_account_key: str,
        target_account_alias: str,
        recorded_at: datetime,
    ) -> None:
        if self._pipeline_event_bus is None:
            return
        trace_id = (
            str(payload.get("trace_id") or "")
            or str(payload.get("signal_id") or "")
            or str(payload.get("intent_id") or "")
            or command_id
        )
        self._pipeline_event_bus.emit(
            PipelineEvent(
                type="command_submitted",
                trace_id=trace_id,
                symbol=str(payload.get("symbol") or ""),
                timeframe=str(payload.get("timeframe") or ""),
                scope=str(payload.get("scope") or "confirmed"),
                ts=recorded_at.isoformat(),
                payload={
                    **payload,
                    "trace_id": trace_id,
                    "command_id": command_id,
                    "command_type": command_type,
                    "action_id": action_id,
                    "command_target_account_key": target_account_key,
                    "target_account_key": target_account_key,
                    "target_account_alias": target_account_alias,
                    "submitted_by_instance_id": self._runtime_identity.instance_id,
                    "submitted_by_run_id": self._runtime_identity.instance_id,
                    "submitted_by_account_key": self._runtime_identity.account_key,
                    "submitted_by_account_alias": self._runtime_identity.account_alias,
                    "instance_role": self._runtime_identity.instance_role,
                },
            )
        )
