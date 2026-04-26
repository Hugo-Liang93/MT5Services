from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from src.config.runtime_identity import RuntimeIdentity
from src.monitoring.pipeline.event_bus import PipelineEvent, PipelineEventBus
from src.trading.commands.results import bind_command_item_result
from src.trading.commands.results import build_operator_command_result
from src.trading.runtime.lifecycle import OwnedThreadLifecycle

logger = logging.getLogger(__name__)


class OperatorCommandConsumer:
    def __init__(
        self,
        *,
        claim_fn,
        complete_fn,
        heartbeat_fn=None,
        mark_dispatched_fn=None,
        runtime_identity: RuntimeIdentity,
        command_service,
        runtime_mode_controller=None,
        exposure_closeout_controller=None,
        pending_entry_manager=None,
        trade_executor=None,
        account_risk_state_projector=None,
        pipeline_event_bus: PipelineEventBus | None = None,
        poll_interval_seconds: float = 0.5,
        batch_size: int = 20,
        lease_seconds: int = 30,
        max_attempts: int = 5,
    ) -> None:
        self._claim_fn = claim_fn
        self._complete_fn = complete_fn
        self._heartbeat_fn = heartbeat_fn
        # §0dm P2：mark_dispatched_fn 在 _execute_command 前 atomic CAS
        # claimed → dispatched，None 时退化为旧行为（仅 lease 过期防御）。
        self._mark_dispatched_fn = mark_dispatched_fn
        self._runtime_identity = runtime_identity
        self._command_service = command_service
        self._runtime_mode_controller = runtime_mode_controller
        self._exposure_closeout_controller = exposure_closeout_controller
        self._pending_entry_manager = pending_entry_manager
        self._trade_executor = trade_executor
        self._account_risk_state_projector = account_risk_state_projector
        self._pipeline_event_bus = pipeline_event_bus
        self._poll_interval_seconds = max(0.1, float(poll_interval_seconds))
        self._batch_size = max(1, int(batch_size))
        self._lease_seconds = max(5, int(lease_seconds))
        self._max_attempts = max(1, int(max_attempts))
        self._worker_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lifecycle = OwnedThreadLifecycle(
            self,
            "_worker_thread",
            label="OperatorCommandConsumer",
        )

    def start(self) -> None:
        if self._lifecycle.is_running():
            return
        self._stop_event.clear()
        self._lifecycle.ensure_running(
            lambda: threading.Thread(
                target=self._worker,
                name="operator-command-consumer",
                daemon=True,
            )
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._lifecycle.stop(self._stop_event, timeout=timeout)

    def is_running(self) -> bool:
        return self._lifecycle.is_running()

    def _worker(self) -> None:
        # §0cc P1：旧实现 _claim_fn / _process_command 异常无顶层 try/except，
        # 一次瞬时 DB 故障就把整个控制面 silently down。本循环必须把单轮异常
        # 隔离在循环体内（log + sleep + retry），不让线程退出。
        while not self._stop_event.is_set():
            try:
                self._worker_iteration()
            except Exception:
                logger.exception(
                    "OperatorCommandConsumer: unexpected error in worker loop; "
                    "continuing after %.2fs poll interval",
                    self._poll_interval_seconds,
                )
                # 用 wait 而非 sleep 让 stop_event 能及时打断
                self._stop_event.wait(self._poll_interval_seconds)

    def _worker_iteration(self) -> None:
        transitions = self._claim_fn(
            target_account_key=self._runtime_identity.account_key,
            claimed_by_instance_id=self._runtime_identity.instance_id,
            claimed_by_run_id=self._runtime_identity.run_id,
            limit=self._batch_size,
            lease_seconds=self._lease_seconds,
            max_attempts=self._max_attempts,
        )
        if isinstance(transitions, dict):
            claimed = list(transitions.get("claimed") or [])
            dead_lettered = list(transitions.get("dead_lettered") or [])
        else:
            claimed = list(transitions or [])
            dead_lettered = []
        # §0dj P2：dead_lettered + claimed 主路径 emit 必须走 _safe_emit_event
        # （§0cc 漏修——_safe_emit_event 已存在但主路径仍直接调原始 _emit_event）。
        for item in dead_lettered:
            self._safe_emit_event(
                "command_failed",
                item,
                extra_payload={
                    "status": "dead_lettered",
                    "error_code": item.get("last_error_code") or "command_attempts_exhausted",
                    "error_message": "operator command dead-lettered after retry exhaustion",
                },
            )
        if not claimed:
            self._stop_event.wait(self._poll_interval_seconds)
            return
        for item in claimed:
            if self._stop_event.is_set():
                return
            self._safe_emit_event("command_claimed", item)
            self._process_command(item)

    def _process_command(self, item: dict[str, Any]) -> None:
        # §0cc P1：旧实现成功分支后失败分支两次调 _complete_fn 都没保护。
        # complete_fn 一次抛 → 异常逃出 _process_command → worker 线程死。
        # 必须把所有 _complete_fn / _project_risk_state / _emit_event 的调用
        # 都包 try/except，让 consumer 在瞬时 DB 故障下持续运行。
        command_id = str(item.get("command_id") or "")
        try:
            self._heartbeat(command_id)
            # §0dm P2：claimed → dispatched atomic CAS。失败 = lease 已被
            # 其他 consumer 抢走，必须放弃执行避免控制面副作用重放污染审计。
            if self._mark_dispatched_fn is not None:
                transitioned = self._mark_dispatched_fn(
                    command_id=command_id,
                    claimed_by_instance_id=self._runtime_identity.instance_id,
                    claimed_by_run_id=self._runtime_identity.run_id,
                )
                if not transitioned:
                    logger.warning(
                        "OperatorCommandConsumer: claimed→dispatched CAS failed "
                        "for command_id=%s (lease taken over before execute); "
                        "abandoning execute to avoid duplicate side effect",
                        command_id,
                    )
                    return
            response_payload = self._execute_command(item)
            audit_id = self._extract_audit_id(response_payload)
            self._safe_complete(
                command_id=command_id,
                status="completed",
                claimed_by_instance_id=self._runtime_identity.instance_id,
                claimed_by_run_id=self._runtime_identity.run_id,
                response_payload=response_payload,
                audit_id=audit_id,
                last_error_code=None,
            )
            self._safe_project_risk_state()
            self._safe_emit_event("command_completed", item, extra_payload=response_payload)
        except Exception as exc:
            logger.exception("Operator command processing failed: %s", command_id)
            error_payload = self._build_failed_response(item, exc)
            audit_id = self._extract_audit_id(error_payload)
            self._safe_complete(
                command_id=command_id,
                status="failed",
                claimed_by_instance_id=self._runtime_identity.instance_id,
                claimed_by_run_id=self._runtime_identity.run_id,
                response_payload=error_payload,
                audit_id=audit_id,
                last_error_code=type(exc).__name__,
            )
            self._safe_project_risk_state()
            self._safe_emit_event("command_failed", item, extra_payload=error_payload)

    def _safe_complete(self, **kwargs: Any) -> None:
        """§0cc P1 + §0dj P2：complete_fn 失败 → lease 过期自然 retry；返
        False 说明 lease 已被其他 consumer 接管，本次回写应丢弃。"""
        try:
            updated = self._complete_fn(**kwargs)
            if updated is False:
                logger.warning(
                    "OperatorCommandConsumer: complete returned 0 rows for "
                    "command_id=%s (lease already taken over by another consumer)",
                    kwargs.get("command_id"),
                )
        except Exception:
            logger.exception(
                "OperatorCommandConsumer: complete_fn failed for command_id=%s; "
                "command will retry on next claim cycle (lease will expire)",
                kwargs.get("command_id"),
            )

    def _safe_project_risk_state(self) -> None:
        try:
            self._project_risk_state()
        except Exception:
            logger.exception(
                "OperatorCommandConsumer: project_risk_state failed; "
                "non-fatal, continuing"
            )

    def _safe_emit_event(self, event_type: str, item: dict[str, Any], **kwargs: Any) -> None:
        try:
            self._emit_event(event_type, item, **kwargs)
        except Exception:
            logger.exception(
                "OperatorCommandConsumer: emit_event(%s) failed; non-fatal",
                event_type,
            )

    def _heartbeat(self, command_id: str) -> None:
        if self._heartbeat_fn is None:
            return
        self._heartbeat_fn(
            command_id=command_id,
            claimed_by_instance_id=self._runtime_identity.instance_id,
            claimed_by_run_id=self._runtime_identity.run_id,
            lease_seconds=self._lease_seconds,
        )

    def _execute_command(self, item: dict[str, Any]) -> dict[str, Any]:
        command_type = str(item.get("command_type") or "").strip()
        payload = dict(item.get("payload") or {})
        action_id = str(item.get("action_id") or "").strip() or command_id_from(item)
        actor = str(item.get("actor") or "").strip() or "operator"
        reason = str(item.get("reason") or "").strip() or None
        idempotency_key = str(item.get("idempotency_key") or "").strip() or None
        request_context = dict(item.get("request_context") or {})

        if command_type == "set_trade_control":
            trade_control = self._command_service.update_trade_control(
                auto_entry_enabled=payload.get("auto_entry_enabled"),
                close_only_mode=payload.get("close_only_mode"),
                reason=reason,
                actor=actor,
                action_id=action_id,
                audit_id=action_id,
                idempotency_key=idempotency_key,
                request_context=request_context,
            )
            if payload.get("reset_circuit") and self._trade_executor is not None:
                self._trade_executor.reset_circuit()
            response_payload = self._wrap_operator_result(
                item=item,
                status="applied",
                message="trade control updated",
                effective_state={"trade_control": trade_control},
            )
            audit_info = self._command_service.record_operator_action(
                command_type=command_type,
                request_payload=self._request_payload(item),
                response_payload=response_payload,
                status="success",
            )
            response_payload["audit_id"] = str(audit_info.get("operation_id") or action_id)
            return response_payload

        if command_type == "set_runtime_mode":
            if self._runtime_mode_controller is None:
                raise RuntimeError("runtime_mode_controller_not_configured")
            snapshot = self._runtime_mode_controller.apply_mode(
                payload.get("mode"),
                reason=reason or "operator_command",
                actor=actor,
                action_id=action_id,
                audit_id=action_id,
                idempotency_key=idempotency_key,
                request_context=request_context,
            )
            runtime_mode = dict(snapshot or {})
            status = "partial_failure" if runtime_mode.get("last_error") else "applied"
            message = (
                f"runtime mode updated with partial failure: {runtime_mode.get('last_error')}"
                if runtime_mode.get("last_error")
                else "runtime mode updated"
            )
            response_payload = self._wrap_operator_result(
                item=item,
                status=status,
                message=message,
                effective_state={"runtime_mode": runtime_mode},
            )
            audit_info = self._command_service.record_operator_action(
                command_type=command_type,
                request_payload=self._request_payload(item),
                response_payload=response_payload,
                status="partial_failure" if status == "partial_failure" else "success",
                error_message=runtime_mode.get("last_error"),
            )
            response_payload["audit_id"] = str(audit_info.get("operation_id") or action_id)
            return response_payload

        if command_type == "reset_circuit_breaker":
            if self._trade_executor is None:
                raise RuntimeError("trade_executor_not_configured")
            self._trade_executor.reset_circuit()
            response_payload = self._wrap_operator_result(
                item=item,
                status="completed",
                message="circuit breaker reset",
                effective_state={
                    "circuit_breaker": {
                        "open": bool(self._trade_executor.circuit_open),
                        "consecutive_failures": int(
                            self._trade_executor.consecutive_failures
                        ),
                        "circuit_open_at": (
                            self._trade_executor.circuit_open_at.isoformat()
                            if self._trade_executor.circuit_open_at is not None
                            else None
                        ),
                    }
                },
            )
            audit_info = self._command_service.record_operator_action(
                command_type=command_type,
                request_payload=self._request_payload(item),
                response_payload=response_payload,
                status="success",
            )
            response_payload["audit_id"] = str(audit_info.get("operation_id") or action_id)
            return response_payload

        if command_type == "close_position":
            return bind_command_item_result(
                item=item,
                result=self._command_service.close_position(
                    ticket=payload.get("ticket"),
                    volume=payload.get("volume"),
                    deviation=int(payload.get("deviation") or 20),
                    comment=str(payload.get("comment") or "close_position"),
                    actor=actor,
                    reason=reason,
                    action_id=action_id,
                    audit_id=action_id,
                    idempotency_key=idempotency_key,
                    request_context=request_context,
                ),
            )

        if command_type == "close_positions_batch":
            return bind_command_item_result(
                item=item,
                result=self._command_service.close_positions_by_tickets(
                    tickets=list(payload.get("tickets") or []),
                    deviation=int(payload.get("deviation") or 20),
                    comment=str(payload.get("comment") or "close_batch"),
                    actor=actor,
                    reason=reason,
                    action_id=action_id,
                    audit_id=action_id,
                    idempotency_key=idempotency_key,
                    request_context=request_context,
                ),
            )

        if command_type == "close_all_positions":
            return bind_command_item_result(
                item=item,
                result=self._command_service.close_all_positions(
                    symbol=payload.get("symbol"),
                    magic=payload.get("magic"),
                    side=payload.get("side"),
                    deviation=int(payload.get("deviation") or 20),
                    comment=str(payload.get("comment") or "close_all_positions"),
                    actor=actor,
                    reason=reason,
                    action_id=action_id,
                    audit_id=action_id,
                    idempotency_key=idempotency_key,
                    request_context=request_context,
                ),
            )

        if command_type == "cancel_orders":
            return bind_command_item_result(
                item=item,
                result=self._command_service.cancel_orders(
                    symbol=payload.get("symbol"),
                    magic=payload.get("magic"),
                    actor=actor,
                    reason=reason,
                    action_id=action_id,
                    audit_id=action_id,
                    idempotency_key=idempotency_key,
                    request_context=request_context,
                ),
            )

        if command_type == "cancel_orders_batch":
            return bind_command_item_result(
                item=item,
                result=self._command_service.cancel_orders_by_tickets(
                    tickets=list(payload.get("tickets") or []),
                    actor=actor,
                    reason=reason,
                    action_id=action_id,
                    audit_id=action_id,
                    idempotency_key=idempotency_key,
                    request_context=request_context,
                ),
            )

        if command_type == "close_exposure":
            if self._exposure_closeout_controller is None:
                raise RuntimeError("exposure_closeout_controller_not_configured")
            closeout = self._exposure_closeout_controller.execute_with_action(
                reason=reason or "manual_risk_off",
                comment=str(payload.get("comment") or "manual_exposure_closeout"),
                actor=actor,
                action_id=action_id,
                audit_id=action_id,
                idempotency_key=idempotency_key,
                request_context=request_context,
            )
            response_payload = self._wrap_operator_result(
                item=item,
                status=str(closeout.get("status") or "completed"),
                message="manual exposure closeout completed"
                if str(closeout.get("status") or "") == "completed"
                else "manual exposure closeout finished with remaining exposure",
                effective_state={"closeout": closeout},
                extra_fields={"closeout": closeout},
            )
            audit_info = self._command_service.record_operator_action(
                command_type=command_type,
                request_payload=self._request_payload(item),
                response_payload=response_payload,
                status="success"
                if str(closeout.get("status") or "") == "completed"
                else "partial_failure",
            )
            response_payload["audit_id"] = str(audit_info.get("operation_id") or action_id)
            return response_payload

        if command_type == "cancel_pending_entry":
            if self._pending_entry_manager is None:
                raise RuntimeError("pending_entry_manager_not_configured")
            cancelled = self._pending_entry_manager.cancel(
                str(payload.get("signal_id") or ""),
                reason=reason or "manual",
            )
            response_payload = self._wrap_operator_result(
                item=item,
                status="completed" if cancelled else "failed",
                message="pending entry cancelled" if cancelled else "pending entry not found",
                effective_state={"pending_entry": {"cancelled": cancelled}},
            )
            audit_info = self._command_service.record_operator_action(
                command_type=command_type,
                request_payload=self._request_payload(item),
                response_payload=response_payload,
                status="success" if cancelled else "failed",
                error_message=None if cancelled else "pending entry not found",
                symbol=payload.get("symbol"),
            )
            response_payload["audit_id"] = str(audit_info.get("operation_id") or action_id)
            return response_payload

        if command_type == "cancel_pending_entries_by_symbol":
            if self._pending_entry_manager is None:
                raise RuntimeError("pending_entry_manager_not_configured")
            cancelled_count = self._pending_entry_manager.cancel_by_symbol(
                str(payload.get("symbol") or ""),
                reason=reason or "manual",
                exclude_direction=payload.get("exclude_direction"),
            )
            response_payload = self._wrap_operator_result(
                item=item,
                status="completed",
                message="pending entries cancelled",
                effective_state={
                    "pending_entries": {
                        "cancelled_count": int(cancelled_count),
                        "symbol": payload.get("symbol"),
                    }
                },
            )
            audit_info = self._command_service.record_operator_action(
                command_type=command_type,
                request_payload=self._request_payload(item),
                response_payload=response_payload,
                status="success",
                symbol=payload.get("symbol"),
            )
            response_payload["audit_id"] = str(audit_info.get("operation_id") or action_id)
            return response_payload

        raise ValueError(f"unsupported operator command: {command_type}")

    def _wrap_operator_result(
        self,
        *,
        item: dict[str, Any],
        status: str,
        message: str,
        effective_state: dict[str, Any],
        extra_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return build_operator_command_result(
            accepted=status not in {"failed"},
            status=status,
            action_id=item.get("action_id"),
            command_id=item.get("command_id"),
            audit_id=item.get("audit_id"),
            actor=item.get("actor"),
            reason=item.get("reason"),
            idempotency_key=item.get("idempotency_key"),
            request_context=dict(item.get("request_context") or {}),
            message=message,
            error_code=None,
            effective_state=effective_state,
            extra_fields=extra_fields,
        )

    def _build_failed_response(
        self,
        item: dict[str, Any],
        exc: Exception,
    ) -> dict[str, Any]:
        return build_operator_command_result(
            accepted=False,
            status="failed",
            action_id=item.get("action_id"),
            command_id=item.get("command_id"),
            audit_id=item.get("audit_id"),
            actor=item.get("actor"),
            reason=item.get("reason"),
            idempotency_key=item.get("idempotency_key"),
            request_context=dict(item.get("request_context") or {}),
            message=str(exc),
            error_code=type(exc).__name__,
            effective_state={},
            error_message=str(exc),
        )

    @staticmethod
    def _request_payload(item: dict[str, Any]) -> dict[str, Any]:
        return {
            **dict(item.get("payload") or {}),
            "action_id": item.get("action_id"),
            "reason": item.get("reason"),
            "actor": item.get("actor"),
            "idempotency_key": item.get("idempotency_key"),
            "request_context": dict(item.get("request_context") or {}),
        }

    @staticmethod
    def _extract_audit_id(response_payload: dict[str, Any]) -> str | None:
        audit_id = response_payload.get("audit_id")
        if audit_id is None:
            return None
        return str(audit_id).strip() or None

    def _project_risk_state(self) -> None:
        if self._account_risk_state_projector is None:
            return
        try:
            self._account_risk_state_projector.project_now()
        except Exception:
            logger.debug("account risk state projection refresh failed", exc_info=True)

    def _emit_event(
        self,
        event_type: str,
        item: dict[str, Any],
        *,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        if self._pipeline_event_bus is None:
            return
        request_payload = dict(item.get("payload") or {})
        response_payload = dict(item.get("response_payload") or {})
        payload = {
            **request_payload,
            "command_id": item.get("command_id"),
            "action_id": item.get("action_id"),
            "signal_id": request_payload.get("signal_id"),
            "intent_id": request_payload.get("intent_id"),
            "target_account_key": item.get("target_account_key"),
            "target_account_alias": item.get("target_account_alias"),
            "command_type": item.get("command_type"),
            "trace_id": self._trace_id_for_item(item),
            "claimed_by_instance_id": self._runtime_identity.instance_id,
            "claimed_by_run_id": self._runtime_identity.run_id,
            "instance_id": self._runtime_identity.instance_id,
            "instance_role": self._runtime_identity.instance_role,
            "account_key": self._runtime_identity.account_key,
            "account_alias": self._runtime_identity.account_alias,
            "response_payload": response_payload,
        }
        if extra_payload:
            payload.update(extra_payload)
        trace_id = (
            self._trace_id_for_item(item)
            or str(payload.get("signal_id") or "")
            or str(payload.get("intent_id") or "")
            or str(item.get("command_id") or "")
        )
        self._pipeline_event_bus.emit(
            PipelineEvent(
                type=event_type,
                trace_id=trace_id,
                symbol=str(payload.get("symbol") or ""),
                timeframe=str(payload.get("timeframe") or ""),
                scope=str(payload.get("scope") or "confirmed"),
                ts=datetime.now(timezone.utc).isoformat(),
                payload=payload,
            )
        )

    @staticmethod
    def _trace_id_for_item(item: dict[str, Any]) -> str:
        request_payload = dict(item.get("payload") or {})
        response_payload = dict(item.get("response_payload") or {})
        return str(
            request_payload.get("trace_id")
            or response_payload.get("trace_id")
            or request_payload.get("signal_id")
            or request_payload.get("intent_id")
            or item.get("command_id")
            or ""
        ).strip()


def command_id_from(item: dict[str, Any]) -> str:
    value = str(item.get("command_id") or "").strip()
    return value or "operator_command"
