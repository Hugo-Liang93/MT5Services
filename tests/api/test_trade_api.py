from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from starlette.requests import Request

from src.api.schemas import (
    AdmissionReportModel,
    BatchCancelOrdersRequest,
    BatchCloseRequest,
    BatchTradeRequest,
    CancelOrdersRequest,
    CloseAllRequest,
    CloseRequest,
    EstimateMarginRequest,
    ExposureCloseoutRequest,
    ModifyOrdersRequest,
    ModifyPositionsRequest,
    RuntimeModeRequest,
    SignalExecuteTradeRequest,
    TradeControlRequest,
    TradeDispatchRequest,
    TradeReconcileRequest,
    TradeRequest,
)
from src.api.trade import (
    cancel_orders,
    cancel_orders_batch,
    close,
    close_all,
    close_batch,
    estimate_margin,
    modify_orders,
    modify_positions,
    orders,
    positions,
    trade,
    trade_active_pending_state_list,
    trade_batch,
    trade_closeout_exposure,
    trade_command_audit_detail,
    trade_command_audits,
    trade_control_status,
    trade_control_update,
    trade_daily_summary,
    trade_dispatch,
    trade_from_signal,
    trade_pending_execution_context_list,
    trade_pending_lifecycle_state_list,
    trade_position_state_list,
    trade_precheck,
    trade_reconcile,
    trade_runtime_mode_status,
    trade_runtime_mode_update,
    trade_state_alerts_summary,
    trade_state_closeout_summary,
    trade_state_stream,
    trade_state_summary,
    trade_trace_by_signal_id,
    trade_trace_by_trace_id,
    trade_traces,
    trading_accounts,
)
from src.clients.base import MT5TradeError
from src.clients.mt5_account import Order, Position
from src.readmodels.runtime import RuntimeReadModel
from src.risk.service import PreTradeRiskBlockedError
from src.trading.admission import TradeAdmissionService
from src.trading.application.idempotency import TradeOperatorActionReplayConflictError


class _FailingTradeService:
    active_account_alias = "live"

    def precheck_trade(self, **kwargs):
        raise MT5TradeError("calendar unavailable")


class _DispatchService:
    active_account_alias = "live"

    def __init__(self) -> None:
        self._control = {
            "auto_entry_enabled": True,
            "close_only_mode": False,
            "updated_at": None,
            "reason": None,
            "actor": None,
            "action_id": None,
            "audit_id": None,
            "idempotency_key": None,
            "request_context": {},
        }
        self.last_dispatch = None
        self.last_recorded_action = None
        self.last_modify_positions = None
        self.update_trade_control_calls = 0
        self.close_position_calls = 0
        self.close_all_calls = 0
        self.close_batch_calls = 0
        self.cancel_orders_calls = 0
        self.cancel_batch_calls = 0
        self._operator_action_replays = {}

    def dispatch_operation(self, operation, payload):
        self.last_dispatch = (operation, payload)
        if operation == "trade":
            return {"ticket": 1, "payload": payload}
        if operation == "blocked_trade":
            raise PreTradeRiskBlockedError(
                "blocked by risk", assessment={"verdict": "block"}
            )
        if operation == "blocked_daily_loss_trade":
            raise PreTradeRiskBlockedError(
                "blocked by risk",
                assessment={
                    "verdict": "block",
                    "checks": [{"name": "daily_loss_limit"}],
                },
            )
        raise ValueError("unsupported trading operation")

    def precheck_trade(self, **kwargs):
        symbol = str(kwargs.get("symbol") or "XAUUSD")
        return {
            "enabled": True,
            "mode": "on",
            "event_blocked": False,
            "calendar_health_degraded": False,
            "blocked": False,
            "verdict": "allow",
            "reason": None,
            "symbol": symbol,
            "active_windows": [],
            "upcoming_windows": [],
            "warnings": [],
            "calendar_health_mode": "warn_only",
            "calendar_health": {"status": "ok"},
            "checks": [{"name": "trade_parameters", "passed": True, "message": "ok"}],
            "estimated_margin": 12.5,
            "margin_error": None,
            "intent": {},
            "request_id": "precheck_req_1",
            "executable": True,
        }

    def daily_trade_summary(self):
        return {"date": "2026-01-01", "total": 0, "success": 0, "failed": 0}

    def trade_control_status(self):
        return dict(self._control)

    def update_trade_control(self, **kwargs):
        self.update_trade_control_calls += 1
        if kwargs.get("auto_entry_enabled") is not None:
            self._control["auto_entry_enabled"] = bool(kwargs["auto_entry_enabled"])
        if kwargs.get("close_only_mode") is not None:
            self._control["close_only_mode"] = bool(kwargs["close_only_mode"])
        self._control["reason"] = kwargs.get("reason") or None
        self._control["actor"] = kwargs.get("actor") or None
        self._control["action_id"] = kwargs.get("action_id") or None
        self._control["audit_id"] = kwargs.get("audit_id") or None
        self._control["idempotency_key"] = kwargs.get("idempotency_key") or None
        self._control["request_context"] = dict(kwargs.get("request_context") or {})
        self._control["updated_at"] = "2026-01-01T00:00:00+00:00"
        return dict(self._control)

    def record_operator_action(self, **kwargs):
        self.last_recorded_action = dict(kwargs)
        operation_id = kwargs.get("operation_id") or "act_1"
        request_payload = dict(kwargs.get("request_payload") or {})
        response_payload = dict(kwargs.get("response_payload") or {})
        if not response_payload.get("action_id"):
            response_payload["action_id"] = operation_id
        if not response_payload.get("audit_id"):
            response_payload["audit_id"] = operation_id
        if not response_payload.get("recorded_at"):
            response_payload["recorded_at"] = "2026-01-01T00:00:00+00:00"
        details = response_payload.get("details")
        if isinstance(details, dict):
            if not details.get("action_id"):
                details["action_id"] = operation_id
            if not details.get("audit_id"):
                details["audit_id"] = operation_id
            if not details.get("recorded_at"):
                details["recorded_at"] = "2026-01-01T00:00:00+00:00"
        idempotency_key = str(request_payload.get("idempotency_key") or "").strip()
        command_type = str(kwargs.get("command_type") or "").strip()
        if command_type and idempotency_key:
            self._operator_action_replays[(command_type, idempotency_key)] = {
                "request_payload": self._normalize_replay_payload(request_payload),
                "response_payload": response_payload,
            }
        return {
            "operation_id": operation_id,
            "recorded_at": "2026-01-01T00:00:00+00:00",
            "status": kwargs.get("status") or "success",
        }

    def find_operator_action_replay(self, **kwargs):
        command_type = str(kwargs.get("command_type") or "").strip()
        idempotency_key = str(kwargs.get("idempotency_key") or "").strip()
        if not command_type or not idempotency_key:
            return None
        replay = self._operator_action_replays.get((command_type, idempotency_key))
        if replay is None:
            return None
        request_payload = self._normalize_replay_payload(
            kwargs.get("request_payload") or {}
        )
        if replay["request_payload"] != request_payload:
            response_payload = dict(replay.get("response_payload") or {})
            raise TradeOperatorActionReplayConflictError(
                (
                    f"idempotency_key '{idempotency_key}' already used for "
                    f"a different {command_type} request"
                ),
                command_type=command_type,
                idempotency_key=idempotency_key,
                existing_record={
                    "action_id": response_payload.get("action_id"),
                    "audit_id": response_payload.get("audit_id"),
                    "recorded_at": response_payload.get("recorded_at"),
                    "request_payload": dict(replay.get("request_payload") or {}),
                    "response_payload": response_payload,
                },
            )
        return {
            "source": "memory",
            "response_payload": dict(replay.get("response_payload") or {}),
        }

    @classmethod
    def _normalize_replay_payload(cls, value):
        if isinstance(value, dict):
            return {
                str(key): cls._normalize_replay_payload(item)
                for key, item in value.items()
                if str(key) not in {"action_id", "audit_id"}
            }
        if isinstance(value, list):
            return [cls._normalize_replay_payload(item) for item in value]
        return value

    def execute_trade(self, **kwargs):
        if kwargs.get("symbol") == "XAUUSD_DAILY_LOSS":
            raise PreTradeRiskBlockedError(
                "blocked by risk",
                assessment={
                    "verdict": "block",
                    "checks": [{"name": "daily_loss_limit"}],
                },
            )
        return {
            "ticket": 1,
            "symbol": kwargs["symbol"],
            "request_id": kwargs.get("request_id") or "req_1",
            "trace_id": "trace_1",
            "operation_id": "op_1",
        }

    def execute_trade_batch(self, trades, stop_on_error=False):
        if any(
            str(trade.get("symbol") or "") == "XAUUSD_BATCH_FAIL" for trade in trades
        ):
            raise MT5TradeError("market closed")
        return {
            "results": [
                {"index": idx, "success": True, "result": trade}
                for idx, trade in enumerate(trades)
            ],
            "success_count": len(trades),
            "failure_count": 0,
            "stop_on_error": stop_on_error,
        }

    def estimate_margin(self, **kwargs):
        if kwargs.get("symbol") == "XAUUSD_MARGIN_FAIL":
            raise MT5TradeError("market closed")
        return {"margin": 512.5}

    def modify_orders(self, **kwargs):
        if kwargs.get("symbol") == "XAUUSD_ORDER_FAIL":
            raise MT5TradeError("order_not_found")
        return {"modified": [11], "failed": []}

    def modify_positions(self, **kwargs):
        self.last_modify_positions = dict(kwargs)
        if kwargs.get("ticket") == 404:
            raise MT5TradeError("position_not_found")
        return {"modified": [kwargs.get("ticket") or 1], "failed": []}

    def _wrap_operator_action_result(
        self, *, command_type: str, request_payload, raw_result
    ):
        action_id = str(request_payload.get("action_id") or f"act_{command_type}_1")
        result_payload = dict(raw_result or {}) if isinstance(raw_result, dict) else {}
        payload = {
            "accepted": True,
            "status": "completed",
            "action_id": action_id,
            "audit_id": str(request_payload.get("audit_id") or action_id),
            "actor": request_payload.get("actor") or "operator",
            "reason": request_payload.get("reason") or None,
            "idempotency_key": request_payload.get("idempotency_key") or None,
            "request_context": dict(request_payload.get("request_context") or {}),
            "message": None,
            "error_code": None,
            "recorded_at": "2026-01-01T00:00:00+00:00",
            "effective_state": {"result": result_payload},
            "result": result_payload,
        }
        if command_type == "close_position":
            success = (
                bool(result_payload.get("success", True)) and raw_result is not None
            )
            payload["accepted"] = success
            payload["status"] = "completed" if success else "failed"
            payload["message"] = (
                "position close completed"
                if success
                else "position close reported failure"
            )
            if not success:
                payload["error_message"] = payload["message"]
                payload["details"] = {"operation": command_type}
        else:
            success_key = (
                "closed"
                if command_type in {"close_all_positions", "close_positions_by_tickets"}
                else "canceled"
            )
            success_items = list(result_payload.get(success_key) or [])
            failed_items = list(result_payload.get("failed") or [])
            if failed_items and success_items:
                payload["status"] = "partial_failure"
            elif failed_items:
                payload["accepted"] = False
                payload["status"] = "failed"
            label = {
                "close_all_positions": "close all positions",
                "close_positions_by_tickets": "batch close",
                "cancel_orders": "cancel orders",
                "cancel_orders_by_tickets": "batch cancel orders",
            }[command_type]
            payload["message"] = {
                "completed": f"{label} completed",
                "partial_failure": f"{label} finished with partial failure",
                "failed": f"{label} failed",
            }[payload["status"]]
            if payload["accepted"] is False:
                payload["error_message"] = payload["message"]
                payload["details"] = {"operation": command_type}
        idempotency_key = str(request_payload.get("idempotency_key") or "").strip()
        if idempotency_key:
            self._operator_action_replays[(command_type, idempotency_key)] = {
                "request_payload": self._normalize_replay_payload(request_payload),
                "response_payload": dict(payload),
            }
        return payload

    def close_position(self, **kwargs):
        self.close_position_calls += 1
        raw_result = {"ticket": kwargs["ticket"], "success": True}
        if (
            kwargs.get("action_id")
            or kwargs.get("idempotency_key")
            or kwargs.get("actor")
        ):
            return self._wrap_operator_action_result(
                command_type="close_position",
                request_payload=kwargs,
                raw_result=raw_result,
            )
        return raw_result

    def close_all_positions(self, **kwargs):
        self.close_all_calls += 1
        raw_result = {"closed": [1], "failed": []}
        if (
            kwargs.get("action_id")
            or kwargs.get("idempotency_key")
            or kwargs.get("actor")
        ):
            return self._wrap_operator_action_result(
                command_type="close_all_positions",
                request_payload=kwargs,
                raw_result=raw_result,
            )
        return raw_result

    def close_positions_by_tickets(self, **kwargs):
        self.close_batch_calls += 1
        raw_result = {"closed": list(kwargs.get("tickets") or []), "failed": []}
        if (
            kwargs.get("action_id")
            or kwargs.get("idempotency_key")
            or kwargs.get("actor")
        ):
            return self._wrap_operator_action_result(
                command_type="close_positions_by_tickets",
                request_payload=kwargs,
                raw_result=raw_result,
            )
        return raw_result

    def cancel_orders(self, **kwargs):
        self.cancel_orders_calls += 1
        raw_result = {"canceled": [11], "failed": []}
        if (
            kwargs.get("action_id")
            or kwargs.get("idempotency_key")
            or kwargs.get("actor")
        ):
            return self._wrap_operator_action_result(
                command_type="cancel_orders",
                request_payload=kwargs,
                raw_result=raw_result,
            )
        return raw_result

    def cancel_orders_by_tickets(self, tickets, **kwargs):
        self.cancel_batch_calls += 1
        request_payload = {"tickets": list(tickets), **kwargs}
        raw_result = {"canceled": list(tickets), "failed": []}
        if (
            kwargs.get("action_id")
            or kwargs.get("idempotency_key")
            or kwargs.get("actor")
        ):
            return self._wrap_operator_action_result(
                command_type="cancel_orders_by_tickets",
                request_payload=request_payload,
                raw_result=raw_result,
            )
        return raw_result

    def account_info(self):
        return {"equity": 10000.0}

    def get_positions(self, symbol=None, magic=None):
        return [
            Position(
                ticket=1,
                symbol=symbol or "XAUUSD",
                volume=0.01,
                price_open=3000.0,
                price_current=3005.0,
                sl=2990.0,
                tp=3020.0,
                profit=5.0,
                swap=0.0,
                time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                type=0,
                magic=magic or 7,
                comment="probe",
            )
        ]

    def get_orders(self, symbol=None, magic=None):
        return [
            Order(
                ticket=2,
                symbol=symbol or "XAUUSD",
                volume=0.01,
                price_open=3001.0,
                price_current=3002.0,
                sl=2991.0,
                tp=3021.0,
                time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                type=0,
                magic=magic or 7,
                comment="probe",
            )
        ]


class _OperatorCommandQueueService:
    def __init__(self) -> None:
        self.last_enqueued = None
        self.enqueue_calls = 0
        self._replays = {}

    def enqueue(
        self,
        *,
        command_type,
        payload,
        actor,
        reason,
        action_id,
        idempotency_key,
        request_context,
        target_account_alias=None,
    ):
        normalized_request = {
            "payload": dict(payload or {}),
            "actor": actor,
            "reason": reason,
            "request_context": dict(request_context or {}),
        }
        replay_key = (
            str(target_account_alias or "live"),
            str(command_type or ""),
            str(idempotency_key or ""),
        )
        if idempotency_key and replay_key in self._replays:
            existing = self._replays[replay_key]
            if existing["request"] != normalized_request:
                raise TradeOperatorActionReplayConflictError(
                    "conflicting operator command replay",
                    command_type=command_type,
                    idempotency_key=idempotency_key,
                    existing_record={
                        "action_id": existing["response"]["action_id"],
                        "audit_id": existing["response"].get("audit_id"),
                        "recorded_at": existing["response"].get("recorded_at"),
                        "request_payload": existing["request"],
                        "response_payload": existing["response"],
                    },
                )
            replayed = dict(existing["response"])
            replayed["replayed"] = True
            return replayed

        self.enqueue_calls += 1
        response = {
            "accepted": True,
            "status": "pending",
            "action_id": action_id,
            "command_id": f"cmd_{self.enqueue_calls}",
            "audit_id": None,
            "actor": actor,
            "reason": reason,
            "idempotency_key": idempotency_key,
            "request_context": dict(request_context or {}),
            "message": "operator command accepted",
            "effective_state": {
                "command_type": command_type,
                "target_account_alias": target_account_alias or "live",
                "target_account_key": "acct_live",
            },
            "recorded_at": "2026-01-01T00:00:00+00:00",
        }
        self.last_enqueued = {
            "command_type": command_type,
            "payload": dict(payload or {}),
            "actor": actor,
            "reason": reason,
            "action_id": action_id,
            "idempotency_key": idempotency_key,
            "request_context": dict(request_context or {}),
            "target_account_alias": target_account_alias or "live",
        }
        if idempotency_key:
            self._replays[replay_key] = {
                "request": normalized_request,
                "response": dict(response),
            }
        return response


def _admission_service(command_service) -> TradeAdmissionService:
    return TradeAdmissionService(
        command_service=command_service,
        runtime_views=RuntimeReadModel(trade_executor=_ExecutorService()),
    )


class _SignalService:
    @staticmethod
    def recent_signals(scope="confirmed", limit=500):
        return [
            {
                "signal_id": "sig_1",
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "strategy": "consensus",
                "direction": "buy",
                "confidence": 0.88,
                "metadata": {"regime": "trend"},
                "indicators_snapshot": {
                    "atr14": {"value": 5.0},
                    "close": {"close": 2350.0},
                },
            }
        ]


class _PaperTradingBridge:
    def status(self):
        return {
            "running": True,
            "session_id": "ps_test_1",
            "started_at": "2026-01-01T00:00:00+00:00",
            "signals_received": 5,
            "signals_executed": 2,
            "signals_rejected": 3,
            "reject_reasons": {"low_confidence": 1, "no_quote": 2},
            "active_symbols": ["XAUUSD"],
            "current_balance": 10020.0,
            "floating_pnl": 5.0,
            "equity": 10025.0,
            "open_positions": 1,
            "closed_trades": 2,
        }


class _ExecutorService:
    def __init__(self) -> None:
        self.reset_calls = 0

    def status(self):
        return {
            "circuit_open": False,
            "execution_quality": {"risk_blocks": 0, "recovered_from_state": 0},
        }

    def reset_circuit(self):
        self.reset_calls += 1


class _PositionManagerService:
    def sync_open_positions(self):
        return {"synced": 1, "recovered": 1, "skipped": 0}

    def status(self):
        return {"tracked_positions": 1, "running": True}

    def active_positions(self):
        return [{"ticket": 1, "signal_id": "sig_1", "symbol": "XAUUSD"}]


class _RuntimeModeController:
    def __init__(self) -> None:
        self.mode = "full"
        self.last_reason = None
        self.last_actor = None
        self.last_action_id = None
        self.last_audit_id = None
        self.last_idempotency_key = None
        self.last_request_context = {}
        self.apply_calls = 0

    def snapshot(self):
        return {
            "current_mode": self.mode,
            "configured_mode": "full",
            "after_eod_action": "ingest_only",
            "components": {"trade_execution": self.mode == "full"},
            "last_transition_reason": self.last_reason,
            "last_actor": self.last_actor,
            "last_action_id": self.last_action_id,
            "last_audit_id": self.last_audit_id,
            "last_idempotency_key": self.last_idempotency_key,
            "last_request_context": dict(self.last_request_context),
        }

    def apply_mode(self, mode: str, *, reason: str, **kwargs):
        self.apply_calls += 1
        self.mode = mode
        self.last_reason = reason
        self.last_actor = kwargs.get("actor")
        self.last_action_id = kwargs.get("action_id")
        self.last_audit_id = kwargs.get("audit_id")
        self.last_idempotency_key = kwargs.get("idempotency_key")
        self.last_request_context = dict(kwargs.get("request_context") or {})
        return self.snapshot()


class _ExposureCloseoutController:
    def __init__(self) -> None:
        self.last_reason = None
        self.last_comment = None
        self.last_actor = None
        self.last_action_id = None
        self.last_audit_id = None
        self.last_idempotency_key = None
        self.last_request_context = {}
        self.execute_calls = 0
        self._status = {
            "status": "idle",
            "last_reason": None,
            "last_comment": None,
            "last_requested_at": None,
            "last_completed_at": None,
            "actor": None,
            "action_id": None,
            "audit_id": None,
            "idempotency_key": None,
            "request_context": {},
            "result": None,
            "runtime_mode_transition": {
                "configured_action": "ingest_only",
                "target_mode": None,
                "applied": False,
                "reason": None,
                "error": None,
                "snapshot": None,
            },
        }

    def execute(self, *, reason: str, comment: str):
        return self.execute_with_action(
            reason=reason,
            comment=comment,
            actor=None,
            action_id=None,
            audit_id=None,
            idempotency_key=None,
            request_context=None,
        )

    def execute_with_action(self, *, reason: str, comment: str, **kwargs):
        self.execute_calls += 1
        self.last_reason = reason
        self.last_comment = comment
        self.last_actor = kwargs.get("actor")
        self.last_action_id = kwargs.get("action_id")
        self.last_audit_id = kwargs.get("audit_id")
        self.last_idempotency_key = kwargs.get("idempotency_key")
        self.last_request_context = dict(kwargs.get("request_context") or {})
        self._status = {
            "status": "completed",
            "last_reason": reason,
            "last_comment": comment,
            "last_requested_at": "2026-01-01T00:00:00+00:00",
            "last_completed_at": "2026-01-01T00:00:00+00:00",
            "actor": self.last_actor,
            "action_id": self.last_action_id,
            "audit_id": self.last_audit_id,
            "idempotency_key": self.last_idempotency_key,
            "request_context": dict(self.last_request_context),
            "result": {
                "completed": True,
                "positions": {
                    "requested": [1],
                    "completed": [1],
                    "failed": [],
                    "error": None,
                },
                "orders": {
                    "requested": [2],
                    "completed": [2],
                    "failed": [],
                    "error": None,
                },
                "remaining_positions": [],
                "remaining_orders": [],
            },
            "runtime_mode_transition": {
                "configured_action": "ingest_only",
                "target_mode": "ingest_only",
                "applied": True,
                "reason": "closeout:manual_risk_off",
                "error": None,
                "snapshot": {"current_mode": "ingest_only"},
            },
        }
        return dict(self._status)

    def status(self):
        return dict(self._status)


class _TradeTraceReadModel:
    def trace_by_signal_id(self, signal_id: str):
        return {
            "signal_id": signal_id,
            "trace_id": "trace_1",
            "found": True,
            "identifiers": {
                "signal_id": signal_id,
                "signal_ids": [signal_id],
                "trace_ids": ["trace_1"],
                "request_ids": [signal_id],
                "operation_ids": ["op_1"],
                "order_tickets": [7001],
                "position_tickets": [8001],
            },
            "summary": {
                "stages": {"confirmed_signal": "present", "trade_outcome": "present"},
                "admission": {
                    "decision": "allow",
                    "stage": "account_risk",
                    "trace_id": "trace_1",
                    "signal_id": signal_id,
                    "reason_count": 1,
                },
            },
            "timeline": [
                {"id": "a", "stage": "signal.confirmed"},
                {"id": "b", "stage": "trade.execute_trade"},
            ],
            "graph": {
                "nodes": [{"id": "a"}, {"id": "b"}],
                "edges": [{"from": "a", "to": "b", "relation": "next"}],
            },
            "facts": {
                "admission_reports": [
                    {
                        "decision": "allow",
                        "stage": "account_risk",
                        "trace_id": "trace_1",
                        "signal_id": signal_id,
                        "reasons": [{"code": "checks_passed", "message": "ok"}],
                    }
                ]
            },
        }

    def trace_by_trace_id(self, trace_id: str):
        return {
            "signal_id": None,
            "trace_id": trace_id,
            "found": True,
            "identifiers": {
                "signal_id": None,
                "signal_ids": [],
                "trace_ids": [trace_id],
                "request_ids": [],
                "operation_ids": [],
                "order_tickets": [],
                "position_tickets": [],
            },
            "summary": {
                "stages": {
                    "pipeline_signal_filter": "present",
                    "pipeline_admission": "present",
                },
                "admission": {
                    "decision": "block",
                    "stage": "account_risk",
                    "trace_id": trace_id,
                    "signal_id": None,
                    "reason_count": 1,
                },
            },
            "timeline": [
                {"id": "a", "stage": "pipeline.bar_closed"},
                {"id": "b", "stage": "pipeline.signal_filter"},
            ],
            "graph": {
                "nodes": [{"id": "a"}, {"id": "b"}],
                "edges": [{"from": "a", "to": "b", "relation": "next"}],
            },
            "facts": {
                "admission_reports": [
                    {
                        "decision": "block",
                        "stage": "account_risk",
                        "trace_id": trace_id,
                        "signal_id": None,
                        "reasons": [
                            {"code": "margin_insufficient", "message": "blocked"}
                        ],
                    }
                ]
            },
        }

    def list_traces(self, **kwargs):
        return {
            "items": [
                {
                    "trace_id": kwargs.get("trace_id") or "trace_1",
                    "signal_id": kwargs.get("signal_id") or "sig_1",
                    "symbol": kwargs.get("symbol") or "XAUUSD",
                    "timeframe": kwargs.get("timeframe") or "M5",
                    "strategy": kwargs.get("strategy") or "consensus",
                    "status": kwargs.get("status") or "submitted",
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "last_event_at": "2026-01-01T00:01:00+00:00",
                    "event_count": 6,
                    "last_stage": "pipeline.execution_submitted",
                    "reason": "ok",
                    "admission": {
                        "decision": "allow",
                        "stage": "account_risk",
                        "trace_id": kwargs.get("trace_id") or "trace_1",
                        "signal_id": kwargs.get("signal_id") or "sig_1",
                        "reason_count": 1,
                    },
                }
            ],
            "total": 5,
            "page": kwargs.get("page") or 1,
            "page_size": kwargs.get("page_size") or 100,
        }


class _PagedTradeQueryService(_DispatchService):
    def command_audit_page(self, **kwargs):
        return {
            "items": [
                {
                    "recorded_at": "2026-01-01T00:00:00+00:00",
                    "operation_id": "op_1",
                    "audit_id": "op_1",
                    "command_type": kwargs.get("command_type") or "closeout_exposure",
                    "status": kwargs.get("status") or "success",
                    "symbol": kwargs.get("symbol") or "XAUUSD",
                    "trace_id": kwargs.get("trace_id") or "trace_1",
                    "signal_id": kwargs.get("signal_id") or "sig_1",
                    "actor": kwargs.get("actor") or "operator",
                    "request_payload": {},
                    "response_payload": {},
                }
            ],
            "total": 9,
            "page": kwargs.get("page") or 1,
            "page_size": kwargs.get("page_size") or 100,
        }


class _StreamingTradeQueryService(_DispatchService):
    def __init__(self) -> None:
        super().__init__()
        self._position_calls = 0
        self._audit_calls = 0

    def get_positions(self, symbol=None, magic=None):
        self._position_calls += 1
        if self._position_calls == 1:
            return []
        return super().get_positions(symbol=symbol, magic=magic)

    def command_audit_page(self, **kwargs):
        self._audit_calls += 1
        return {"items": [], "total": 0, "page": 1, "page_size": 50}


class _StreamingTradeControlService(_DispatchService):
    def __init__(self) -> None:
        super().__init__()
        self._trade_control_calls = 0

    def trade_control_status(self):
        self._trade_control_calls += 1
        if self._trade_control_calls == 1:
            return {
                "auto_entry_enabled": True,
                "close_only_mode": False,
                "updated_at": "2026-01-01T00:00:00+00:00",
                "reason": None,
                "actor": None,
                "action_id": None,
                "audit_id": None,
                "idempotency_key": None,
                "request_context": {},
            }
        return {
            "auto_entry_enabled": False,
            "close_only_mode": True,
            "updated_at": "2026-01-01T00:00:01+00:00",
            "reason": "nfp_window",
            "actor": "operator",
            "action_id": "act_trade_control_1",
            "audit_id": "act_trade_control_1",
            "idempotency_key": "idem_trade_control_1",
            "request_context": {"panel": "execution"},
        }

    def command_audit_page(self, **kwargs):
        return {"items": [], "total": 0, "page": 1, "page_size": 50}


class _StreamingStaticTradeQueryService(_DispatchService):
    def get_positions(self, symbol=None, magic=None):
        return []

    def command_audit_page(self, **kwargs):
        return {"items": [], "total": 0, "page": 1, "page_size": 50}


def test_trade_precheck_wraps_mt5_errors() -> None:
    response = trade_precheck(
        TradeRequest(symbol="XAUUSD", volume=0.1, side="buy"),
        service=_FailingTradeService(),
        admission_service=_admission_service(_FailingTradeService()),
    )

    assert response.success is False
    assert response.error is not None
    assert response.error["message"] == "Trade precheck failed: calendar unavailable"
    assert response.error["details"]["account_alias"] == "live"


def test_trade_dispatch_uses_unified_dispatcher() -> None:
    service = _DispatchService()
    response = trade_dispatch(
        TradeDispatchRequest(
            operation="trade",
            payload={"symbol": "XAUUSD", "volume": 0.1, "side": "buy"},
        ),
        service=service,
        admission_service=_admission_service(service),
    )

    assert response.success is True
    assert response.data["result"]["ticket"] == 1
    assert response.data["admission_report"]["decision"] == "allow"


def test_trade_precheck_returns_admission_report() -> None:
    service = _DispatchService()

    response = trade_precheck(
        TradeRequest(symbol="XAUUSD", volume=0.1, side="buy"),
        service=service,
        admission_service=_admission_service(service),
    )

    assert response.success is True
    assert isinstance(response.data, AdmissionReportModel)
    assert response.data.decision == "allow"
    assert response.data.requested_operation == "trade_precheck"
    assert response.metadata["operation"] == "trade_precheck"


def test_trade_daily_summary_endpoint() -> None:
    response = trade_daily_summary(service=_DispatchService())

    assert response.success is True
    assert response.data["date"] == "2026-01-01"


def test_trade_control_status_endpoint() -> None:
    executor = _ExecutorService()
    response = trade_control_status(
        service=_DispatchService(),
        runtime_views=RuntimeReadModel(trade_executor=executor),
    )

    assert response.success is True
    assert response.data["trade_control"]["auto_entry_enabled"] is True
    assert response.data["persisted_trade_control"] is None
    assert response.data["executor"]["status"] == "disabled"
    assert response.metadata["operation"] == "trade_control_status"


def test_trade_control_update_endpoint_can_reset_circuit() -> None:
    queue = _OperatorCommandQueueService()

    response = trade_control_update(
        TradeControlRequest(
            auto_entry_enabled=False,
            close_only_mode=True,
            reason="nfp_window",
            reset_circuit=True,
            actor="operator",
            idempotency_key="idem_trade_control_1",
            request_context={"panel": "execution"},
        ),
        command_queue=queue,
    )

    assert response.success is True
    assert response.data["accepted"] is True
    assert response.data["status"] == "pending"
    assert response.data["command_id"] is not None
    assert response.data["actor"] == "operator"
    assert response.data["idempotency_key"] == "idem_trade_control_1"
    assert response.metadata["reset_circuit"] is True
    assert queue.last_enqueued["command_type"] == "set_trade_control"


def test_trade_control_update_endpoint_replays_same_idempotency_key() -> None:
    queue = _OperatorCommandQueueService()
    request = TradeControlRequest(
        auto_entry_enabled=False,
        close_only_mode=True,
        reason="nfp_window",
        reset_circuit=True,
        actor="operator",
        idempotency_key="idem_trade_control_replay",
        request_context={"panel": "execution"},
    )

    first = trade_control_update(
        request,
        command_queue=queue,
    )
    second = trade_control_update(
        request,
        command_queue=queue,
    )

    assert first.success is True
    assert second.success is True
    assert second.metadata["replayed"] is True
    assert second.data["action_id"] == first.data["action_id"]
    assert queue.enqueue_calls == 1


def test_trade_control_update_endpoint_rejects_conflicting_idempotency_reuse() -> None:
    queue = _OperatorCommandQueueService()
    first = trade_control_update(
        TradeControlRequest(
            auto_entry_enabled=False,
            close_only_mode=True,
            reason="nfp_window",
            reset_circuit=False,
            actor="operator",
            idempotency_key="idem_trade_control_conflict",
            request_context={"panel": "execution"},
        ),
        command_queue=queue,
    )
    second = trade_control_update(
        TradeControlRequest(
            auto_entry_enabled=True,
            close_only_mode=False,
            reason="resume",
            reset_circuit=False,
            actor="operator",
            idempotency_key="idem_trade_control_conflict",
            request_context={"panel": "execution"},
        ),
        command_queue=queue,
    )

    assert first.success is True
    assert second.success is False
    assert second.error["code"] == "invalid_request"
    assert second.error["details"]["existing_action_id"] == first.data["action_id"]
    assert queue.enqueue_calls == 1


def test_close_endpoint_returns_unified_action_result_and_replays_same_idempotency_key() -> (
    None
):
    queue = _OperatorCommandQueueService()
    request = CloseRequest(
        ticket=1001,
        volume=0.01,
        reason="manual_close",
        actor="operator",
        idempotency_key="idem_close_1",
        request_context={"panel": "positions"},
    )

    first = close(request, command_queue=queue)
    second = close(request, command_queue=queue)

    assert first.success is True
    assert first.data["accepted"] is True
    assert first.data["status"] == "pending"
    assert first.data["command_id"] is not None
    assert second.success is True
    assert second.metadata["replayed"] is True
    assert second.data["action_id"] == first.data["action_id"]
    assert queue.enqueue_calls == 1


def test_close_all_endpoint_returns_unified_action_result() -> None:
    queue = _OperatorCommandQueueService()

    response = close_all(
        CloseAllRequest(
            symbol="XAUUSD",
            reason="manual_close_all",
            actor="operator",
            idempotency_key="idem_close_all_1",
            request_context={"panel": "positions"},
        ),
        command_queue=queue,
    )

    assert response.success is True
    assert response.data["accepted"] is True
    assert response.data["status"] == "pending"
    assert response.data["command_id"] is not None
    assert response.metadata["operation"] == "close_all_positions"
    assert queue.last_enqueued["command_type"] == "close_all_positions"


def test_close_batch_endpoint_rejects_conflicting_idempotency_reuse() -> None:
    queue = _OperatorCommandQueueService()
    first = close_batch(
        BatchCloseRequest(
            tickets=[1001, 1002],
            reason="manual_close_batch",
            actor="operator",
            idempotency_key="idem_close_batch_conflict",
            request_context={"panel": "positions"},
        ),
        command_queue=queue,
    )
    second = close_batch(
        BatchCloseRequest(
            tickets=[2001],
            reason="manual_close_batch",
            actor="operator",
            idempotency_key="idem_close_batch_conflict",
            request_context={"panel": "positions"},
        ),
        command_queue=queue,
    )

    assert first.success is True
    assert second.success is False
    assert second.error["code"] == "invalid_request"
    assert second.error["details"]["existing_action_id"] == first.data["action_id"]
    assert queue.enqueue_calls == 1


def test_cancel_orders_endpoint_returns_unified_action_result_and_replays_same_idempotency_key() -> (
    None
):
    queue = _OperatorCommandQueueService()
    request = CancelOrdersRequest(
        symbol="XAUUSD",
        magic=7,
        reason="manual_cancel_orders",
        actor="operator",
        idempotency_key="idem_cancel_orders_1",
        request_context={"panel": "orders"},
    )

    first = cancel_orders(request, command_queue=queue)
    second = cancel_orders(request, command_queue=queue)

    assert first.success is True
    assert first.data["accepted"] is True
    assert first.data["status"] == "pending"
    assert first.data["command_id"] is not None
    assert second.success is True
    assert second.metadata["replayed"] is True
    assert second.data["action_id"] == first.data["action_id"]
    assert queue.enqueue_calls == 1


def test_cancel_orders_batch_endpoint_returns_unified_action_result() -> None:
    queue = _OperatorCommandQueueService()

    response = cancel_orders_batch(
        BatchCancelOrdersRequest(
            tickets=[2001, 2002],
            reason="manual_cancel_orders_batch",
            actor="operator",
            idempotency_key="idem_cancel_orders_batch_1",
            request_context={"panel": "orders"},
        ),
        command_queue=queue,
    )

    assert response.success is True
    assert response.data["accepted"] is True
    assert response.data["status"] == "pending"
    assert response.data["command_id"] is not None
    assert response.metadata["operation"] == "cancel_orders_batch"
    assert queue.last_enqueued["command_type"] == "cancel_orders_batch"


def test_trade_reconcile_endpoint_returns_manager_snapshot() -> None:
    manager = _PositionManagerService()
    response = trade_reconcile(
        TradeReconcileRequest(sync_open_positions=True),
        manager=manager,
        runtime_views=RuntimeReadModel(position_manager=manager),
    )

    assert response.success is True
    assert response.data["reconcile"]["recovered"] == 1
    assert response.data["position_manager"]["tracked_positions"] == 1
    assert response.data["tracked_positions"]["items"][0]["symbol"] == "XAUUSD"
    assert "trading_state" in response.data


class _TradingStateStore:
    def load_trade_control_state(self):
        return {"auto_entry_enabled": False, "close_only_mode": True}

    def list_pending_order_states(self, *, statuses=None, limit=100):
        rows = [
            {"order_ticket": 101, "status": "placed", "symbol": "XAUUSD"},
            {"order_ticket": 102, "status": "orphan", "symbol": "XAUUSD"},
            {"order_ticket": 103, "status": "filled", "symbol": "XAUUSD"},
        ]
        if statuses:
            rows = [row for row in rows if row["status"] in set(statuses)]
        return rows[:limit]

    def list_position_runtime_states(self, *, statuses=None, limit=100):
        rows = [
            {"position_ticket": 201, "status": "open", "symbol": "XAUUSD"},
            {"position_ticket": 202, "status": "closed", "symbol": "XAUUSD"},
        ]
        if statuses:
            rows = [row for row in rows if row["status"] in set(statuses)]
        return rows[:limit]


class _TradingStateAlerts:
    def summary(self):
        return {
            "status": "warning",
            "alerts": [{"code": "pending_orphan", "severity": "warning"}],
            "summary": [{"code": "pending_orphan", "status": "failed"}],
            "observed": {"active_pending_count": 2},
        }


class _RuntimeIdentity:
    environment = "live"
    instance_id = "live-main"
    instance_role = "main"
    account_key = "live_main"
    account_alias = "live_main"


class _StreamingPipelineTraceWriter:
    def __init__(self) -> None:
        self._calls = 0

    def fetch_pipeline_trace_filtered(self, **kwargs):
        self._calls += 1
        if self._calls == 1:
            return []
        return [
            {
                "id": 101,
                "trace_id": "trace_pipeline_1",
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "scope": "confirmed",
                "event_type": "admission_report_appended",
                "recorded_at": datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
                "payload": {"decision": "block", "stage": "account_risk"},
                "instance_id": kwargs.get("instance_id"),
                "instance_role": "main",
                "account_key": kwargs.get("account_key"),
                "signal_id": "sig_pipeline_1",
                "intent_id": "intent_pipeline_1",
                "command_id": None,
                "action_id": None,
            }
        ]


class _PendingEntryManager:
    def active_execution_contexts(self):
        return [
            {
                "signal_id": "sig_1",
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "strategy": "sma_trend",
                "direction": "buy",
                "source": "pending_entry",
            },
            {
                "signal_id": "sig_2",
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "strategy": "sma_trend",
                "direction": "buy",
                "source": "mt5_order",
            },
        ]


def test_trade_state_summary_endpoint_returns_persisted_state() -> None:
    response = trade_state_summary(
        runtime_views=RuntimeReadModel(
            trading_state_store=_TradingStateStore(),
            trading_state_alerts=_TradingStateAlerts(),
            exposure_closeout_controller=_ExposureCloseoutController(),
            runtime_mode_controller=_RuntimeModeController(),
            paper_trading_bridge=_PaperTradingBridge(),
        ),
    )

    assert response.success is True
    assert response.data["trade_control"]["close_only_mode"] is True
    assert response.data["runtime_mode"]["current_mode"] == "full"
    assert response.data["closeout"]["status"] == "idle"
    assert response.data["pending"]["active"]["status_counts"]["placed"] == 1
    assert response.data["pending"]["lifecycle"]["status_counts"]["filled"] == 1
    assert response.data["positions"]["status_counts"]["open"] == 1
    assert response.data["alerts"]["status"] == "warning"
    assert response.data["validation"]["paper_trading"]["kind"] == "validation_sidecar"
    assert response.data["validation"]["paper_trading"]["signals_rejected"] == 3


def test_trading_accounts_endpoint_reads_runtime_identity_via_public_port(
    monkeypatch,
) -> None:
    import src.api.trade_routes.state_routes.overview as trade_state_module
    from src.config.mt5 import MT5Settings

    monkeypatch.setattr(
        trade_state_module,
        "list_mt5_accounts",
        lambda: [
            MT5Settings(
                instance_name="live-main",
                account_alias="live_main",
                account_label="Live Main",
                mt5_login=50256386,
                mt5_server="TradeMaxGlobal-Live",
                timezone="UTC",
                enabled=True,
            )
        ],
    )

    response = trading_accounts(
        service=_DispatchService(),
        runtime_views=RuntimeReadModel(runtime_identity=_RuntimeIdentity()),
    )

    assert response.success is True
    assert response.data[0].alias == "live_main"
    assert response.data[0].environment == "live"
    assert response.metadata["operation"] == "trading_accounts"


def test_trade_trace_endpoint_returns_flow_projection() -> None:
    response = trade_trace_by_signal_id(
        "sig_1",
        trace_views=_TradeTraceReadModel(),
    )

    assert response.success is True
    assert response.data["signal_id"] == "sig_1"
    assert response.data["trace_id"] == "trace_1"
    assert response.data["identifiers"]["order_tickets"] == [7001]
    assert response.data["summary"]["admission"]["decision"] == "allow"
    assert response.data["facts"]["admission_reports"][0]["stage"] == "account_risk"
    assert response.data["graph"]["edges"][0]["relation"] == "next"
    assert response.metadata["operation"] == "trade_trace_by_signal_id"


def test_trade_trace_by_trace_id_endpoint_returns_flow_projection() -> None:
    response = trade_trace_by_trace_id(
        "trace_1",
        trace_views=_TradeTraceReadModel(),
    )

    assert response.success is True
    assert response.data["trace_id"] == "trace_1"
    assert response.data["timeline"][0]["stage"] == "pipeline.bar_closed"
    assert response.data["summary"]["stages"]["pipeline_signal_filter"] == "present"
    assert response.data["summary"]["stages"]["pipeline_admission"] == "present"
    assert response.data["summary"]["admission"]["decision"] == "block"
    assert response.metadata["operation"] == "trade_trace_by_trace_id"


def test_trade_traces_endpoint_returns_directory_projection() -> None:
    response = trade_traces(
        symbol="XAUUSD",
        timeframe="M5",
        status="submitted",
        page=2,
        page_size=20,
        sort="last_event_at_desc",
        trace_views=_TradeTraceReadModel(),
    )

    assert response.success is True
    assert response.data[0].trace_id == "trace_1"
    assert response.data[0].status == "submitted"
    assert response.data[0].admission.decision == "allow"
    assert response.metadata["operation"] == "trade_traces"
    assert response.metadata["page"] == 2
    assert response.metadata["total"] == 5


def test_trade_state_alerts_summary_endpoint_returns_alert_projection() -> None:
    response = trade_state_alerts_summary(
        runtime_views=RuntimeReadModel(trading_state_alerts=_TradingStateAlerts()),
    )

    assert response.success is True
    assert response.data["status"] == "warning"


def test_trade_state_closeout_summary_endpoint_returns_projection() -> None:
    controller = _ExposureCloseoutController()
    controller.execute(reason="manual_risk_off", comment="manual_exposure_closeout")
    response = trade_state_closeout_summary(
        runtime_views=RuntimeReadModel(exposure_closeout_controller=controller),
    )

    assert response.success is True
    assert response.data["status"] == "completed"
    assert response.data["result"]["orders"]["completed"] == [2]
    assert response.data["runtime_mode_transition"]["target_mode"] == "ingest_only"


def test_trade_command_audits_endpoint_exposes_pagination_filters() -> None:
    response = trade_command_audits(
        command_type="closeout_exposure",
        status="success",
        symbol="XAUUSD",
        signal_id="sig_1",
        trace_id="trace_1",
        actor="operator",
        page=3,
        page_size=25,
        sort="recorded_at_asc",
        service=_PagedTradeQueryService(),
    )

    assert response.success is True
    assert response.data[0]["audit_id"] == "op_1"
    assert response.metadata["page"] == 3
    assert response.metadata["page_size"] == 25
    assert response.metadata["total"] == 9
    assert response.metadata["trace_id"] == "trace_1"


class _AuditReceiptLookupService(_DispatchService):
    """P10.4: 验证 audit_id / action_id / idempotency_key 透传 + detail 调用。"""

    def __init__(self) -> None:
        super().__init__()
        self.page_kwargs: dict | None = None
        self.detail_audit_id: str | None = None

    def command_audit_page(self, **kwargs):
        self.page_kwargs = kwargs
        return {
            "items": [
                {
                    "recorded_at": "2026-04-20T12:00:00+00:00",
                    "operation_id": "op_42",
                    "audit_id": "op_42",
                    "command_type": "set_trade_control",
                    "status": "success",
                    "action_id": kwargs.get("action_id") or "act_42",
                    "idempotency_key": kwargs.get("idempotency_key") or "idem_42",
                }
            ],
            "total": 1,
            "page": kwargs.get("page") or 1,
            "page_size": kwargs.get("page_size") or 100,
        }

    def command_audit_detail(self, *, audit_id: str):
        self.detail_audit_id = audit_id
        if audit_id == "missing":
            return None
        return {
            "recorded_at": "2026-04-20T12:00:00+00:00",
            "operation_id": audit_id,
            "audit_id": audit_id,
            "command_type": "set_trade_control",
            "status": "success",
            "action_id": "act_42",
            "idempotency_key": "idem_42",
            "reason": "after_hours",
            "linked_operator_command": {
                "command_id": "cmd_42",
                "status": "completed",
                "action_id": "act_42",
                "idempotency_key": "idem_42",
                "reason": "after_hours",
                "attempt_count": 1,
                "audit_id": audit_id,
            },
        }


def test_trade_command_audits_endpoint_passes_receipt_filters() -> None:
    service = _AuditReceiptLookupService()

    response = trade_command_audits(
        audit_id="op_42",
        action_id="act_42",
        idempotency_key="idem_42",
        service=service,
    )

    assert response.success is True
    assert service.page_kwargs is not None
    assert service.page_kwargs["audit_id"] == "op_42"
    assert service.page_kwargs["action_id"] == "act_42"
    assert service.page_kwargs["idempotency_key"] == "idem_42"
    assert response.metadata["audit_id"] == "op_42"
    assert response.metadata["action_id"] == "act_42"
    assert response.metadata["idempotency_key"] == "idem_42"


def test_trade_command_audit_detail_returns_linked_operator_command() -> None:
    service = _AuditReceiptLookupService()

    response = trade_command_audit_detail(audit_id="op_42", service=service)

    assert response.success is True
    assert response.data["audit_id"] == "op_42"
    assert response.data["linked_operator_command"]["command_id"] == "cmd_42"
    assert response.metadata["has_linked_command"] is True
    assert service.detail_audit_id == "op_42"


def test_trade_command_audit_detail_returns_not_found_for_missing_audit() -> None:
    service = _AuditReceiptLookupService()

    response = trade_command_audit_detail(audit_id="missing", service=service)

    assert response.success is False
    assert response.error is not None
    assert response.error["code"] == "not_found"


def test_trade_command_audit_detail_rejects_blank_audit_id() -> None:
    service = _AuditReceiptLookupService()

    response = trade_command_audit_detail(audit_id="   ", service=service)

    assert response.success is False
    assert response.error is not None
    assert response.error["code"] == "validation_error"
    assert service.detail_audit_id is None


def test_trade_runtime_mode_status_endpoint_returns_projection() -> None:
    response = trade_runtime_mode_status(
        runtime_views=RuntimeReadModel(
            runtime_mode_controller=_RuntimeModeController(),
        )
    )

    assert response.success is True
    assert response.data["current_mode"] == "full"


def test_trade_runtime_mode_update_endpoint_applies_mode() -> None:
    queue = _OperatorCommandQueueService()

    response = trade_runtime_mode_update(
        RuntimeModeRequest(
            mode="risk_off",
            reason="after_hours",
            actor="operator",
            idempotency_key="idem_runtime_mode_1",
            request_context={"panel": "overview"},
        ),
        command_queue=queue,
    )

    assert response.success is True
    assert response.data["accepted"] is True
    assert response.data["status"] == "pending"
    assert response.data["command_id"] is not None
    assert response.data["actor"] == "operator"
    assert response.metadata["operation"] == "trade_runtime_mode_update"
    assert queue.last_enqueued["command_type"] == "set_runtime_mode"


def test_trade_runtime_mode_update_endpoint_replays_same_idempotency_key() -> None:
    queue = _OperatorCommandQueueService()
    request = RuntimeModeRequest(
        mode="risk_off",
        reason="after_hours",
        actor="operator",
        idempotency_key="idem_runtime_mode_replay",
        request_context={"panel": "overview"},
    )

    first = trade_runtime_mode_update(
        request,
        command_queue=queue,
    )
    second = trade_runtime_mode_update(
        request,
        command_queue=queue,
    )

    assert first.success is True
    assert second.success is True
    assert second.metadata["replayed"] is True
    assert second.data["action_id"] == first.data["action_id"]
    assert queue.enqueue_calls == 1


def test_trade_closeout_exposure_endpoint_executes_unified_controller() -> None:
    queue = _OperatorCommandQueueService()

    response = trade_closeout_exposure(
        ExposureCloseoutRequest(
            reason="manual_risk_off",
            comment="manual_exposure_closeout",
            actor="operator",
            idempotency_key="idem_closeout_1",
            request_context={"panel": "execution"},
        ),
        command_queue=queue,
    )

    assert response.success is True
    assert response.data["accepted"] is True
    assert response.data["status"] == "pending"
    assert response.data["command_id"] is not None
    assert response.data["actor"] == "operator"
    assert response.metadata["operation"] == "trade_closeout_exposure"
    assert queue.last_enqueued["command_type"] == "close_exposure"


def test_trade_closeout_exposure_endpoint_replays_same_idempotency_key() -> None:
    queue = _OperatorCommandQueueService()
    request = ExposureCloseoutRequest(
        reason="manual_risk_off",
        comment="manual_exposure_closeout",
        actor="operator",
        idempotency_key="idem_closeout_replay",
        request_context={"panel": "execution"},
    )

    first = trade_closeout_exposure(
        request,
        command_queue=queue,
    )
    second = trade_closeout_exposure(
        request,
        command_queue=queue,
    )

    assert first.success is True
    assert second.success is True
    assert second.metadata["replayed"] is True
    assert second.data["action_id"] == first.data["action_id"]
    assert queue.enqueue_calls == 1


def test_trade_pending_lifecycle_state_list_endpoint_filters_status() -> None:
    response = trade_pending_lifecycle_state_list(
        status="orphan",
        limit=20,
        runtime_views=RuntimeReadModel(trading_state_store=_TradingStateStore()),
    )

    assert response.success is True
    assert response.data["count"] == 1
    assert response.data["items"][0]["status"] == "orphan"
    assert response.metadata["operation"] == "trade_pending_lifecycle_state_list"


def test_trade_active_pending_state_list_endpoint_returns_active_projection() -> None:
    response = trade_active_pending_state_list(
        limit=20,
        runtime_views=RuntimeReadModel(trading_state_store=_TradingStateStore()),
    )

    assert response.success is True
    assert response.data["view"] == "active"
    assert response.data["count"] == 2
    assert response.metadata["operation"] == "trade_active_pending_state_list"


def test_trade_pending_execution_context_list_endpoint_returns_runtime_projection() -> (
    None
):
    response = trade_pending_execution_context_list(
        runtime_views=RuntimeReadModel(pending_entry_manager=_PendingEntryManager()),
    )

    assert response.success is True
    assert response.data["view"] == "execution_contexts"
    assert response.data["source_counts"]["pending_entry"] == 1
    assert response.data["source_counts"]["mt5_order"] == 1
    assert response.metadata["operation"] == "trade_pending_execution_context_list"


def test_trade_position_state_list_endpoint_filters_status() -> None:
    response = trade_position_state_list(
        status="open",
        limit=20,
        runtime_views=RuntimeReadModel(trading_state_store=_TradingStateStore()),
    )

    assert response.success is True
    assert response.data["count"] == 1
    assert response.data["items"][0]["status"] == "open"
    assert response.metadata["operation"] == "trade_position_state_list"


def test_trade_dispatch_returns_risk_block_error() -> None:
    service = _DispatchService()
    response = trade_dispatch(
        TradeDispatchRequest(operation="blocked_trade", payload={}),
        service=service,
        admission_service=_admission_service(service),
    )

    assert response.success is False
    assert response.error["code"] == "trade_blocked_by_risk"


def test_trade_dispatch_returns_daily_loss_limit_error() -> None:
    service = _DispatchService()
    response = trade_dispatch(
        TradeDispatchRequest(operation="blocked_daily_loss_trade", payload={}),
        service=service,
        admission_service=_admission_service(service),
    )

    assert response.success is False
    assert response.error["code"] == "daily_loss_limit"


def test_trade_dispatch_normalizes_trade_alias_and_direction() -> None:
    service = _DispatchService()

    response = trade_dispatch(
        TradeDispatchRequest(
            operation="submit_trade",
            payload={"symbol": "XAUUSD", "volume": 0.1, "direction": "buy"},
        ),
        service=service,
        admission_service=_admission_service(service),
    )

    assert response.success is True
    assert response.metadata["operation"] == "trade"
    assert response.metadata["requested_operation"] == "submit_trade"
    assert response.data["admission_report"]["decision"] == "allow"
    assert service.last_dispatch == (
        "trade",
        {
            "symbol": "XAUUSD",
            "volume": 0.1,
            "side": "buy",
            "order_kind": "market",
            "deviation": 20,
            "comment": "",
            "magic": 0,
            "dry_run": False,
        },
    )


def test_trade_state_stream_emits_snapshot_then_position_change() -> None:
    import src.api.trade_routes.state_routes.stream as trade_state_module

    trade_state_module._TRADE_STATE_STREAM_POLL_SECONDS = 0.01
    runtime_views = RuntimeReadModel(
        trading_state_store=_TradingStateStore(),
        trading_state_alerts=_TradingStateAlerts(),
        exposure_closeout_controller=_ExposureCloseoutController(),
        runtime_mode_controller=_RuntimeModeController(),
    )
    response = asyncio.run(
        trade_state_stream(
            Request({"type": "http", "headers": []}),
            include_snapshot=True,
            heartbeat_seconds=30,
            runtime_views=runtime_views,
            service=_StreamingTradeQueryService(),
        )
    )

    async def _consume() -> list[str]:
        body_iterator = response.body_iterator
        chunks = [
            await body_iterator.__anext__(),
            await body_iterator.__anext__(),
        ]
        await body_iterator.aclose()
        return [
            chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks
        ]

    chunks = asyncio.run(_consume())
    assert "event: state_snapshot" in chunks[0]
    assert "event: position_changed" in chunks[1]


def test_trade_state_stream_emits_trade_control_change_with_action_ids() -> None:
    import src.api.trade_routes.state_routes.stream as trade_state_module

    trade_state_module._TRADE_STATE_STREAM_POLL_SECONDS = 0.01
    runtime_views = RuntimeReadModel(
        trading_state_store=_TradingStateStore(),
        trading_state_alerts=_TradingStateAlerts(),
        exposure_closeout_controller=_ExposureCloseoutController(),
        runtime_mode_controller=_RuntimeModeController(),
    )
    response = asyncio.run(
        trade_state_stream(
            Request({"type": "http", "headers": []}),
            include_snapshot=True,
            heartbeat_seconds=30,
            runtime_views=runtime_views,
            service=_StreamingTradeControlService(),
        )
    )

    async def _consume() -> list[str]:
        body_iterator = response.body_iterator
        chunks = [
            await body_iterator.__anext__(),
            await body_iterator.__anext__(),
        ]
        await body_iterator.aclose()
        return [
            chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks
        ]

    chunks = asyncio.run(_consume())
    assert "event: state_snapshot" in chunks[0]
    assert "event: trade_control_changed" in chunks[1]
    assert '"action_id": "act_trade_control_1"' in chunks[1]


def test_trade_state_stream_emits_pipeline_event_from_formal_trace_source() -> None:
    import src.api.trade_routes.state_routes.stream as trade_state_module

    trade_state_module._TRADE_STATE_STREAM_POLL_SECONDS = 0.01
    runtime_views = RuntimeReadModel(
        trading_state_store=_TradingStateStore(),
        trading_state_alerts=_TradingStateAlerts(),
        exposure_closeout_controller=_ExposureCloseoutController(),
        runtime_mode_controller=_RuntimeModeController(),
        runtime_identity=_RuntimeIdentity(),
        db_writer=_StreamingPipelineTraceWriter(),
    )
    response = asyncio.run(
        trade_state_stream(
            Request({"type": "http", "headers": []}),
            include_snapshot=True,
            heartbeat_seconds=30,
            runtime_views=runtime_views,
            service=_StreamingStaticTradeQueryService(),
        )
    )

    async def _consume() -> list[str]:
        body_iterator = response.body_iterator
        chunks = [
            await body_iterator.__anext__(),
            await body_iterator.__anext__(),
        ]
        await body_iterator.aclose()
        return [
            chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks
        ]

    chunks = asyncio.run(_consume())
    assert "event: state_snapshot" in chunks[0]
    assert "event: admission_report_appended" in chunks[1]
    assert '"trace_id": "trace_pipeline_1"' in chunks[1]
    assert '"pipeline_event_id": 101' in chunks[1]


def test_trade_endpoint_exposes_standardized_observability_metadata() -> None:
    response = trade(
        TradeRequest(
            symbol="XAUUSD", volume=0.1, side="buy", dry_run=True, request_id="req_x"
        ),
        service=_DispatchService(),
    )

    assert response.success is True
    assert response.metadata["request_id"] == "req_x"
    assert response.metadata["trace_id"] == "trace_1"
    assert response.metadata["operation_id"] == "op_1"


def test_trade_endpoint_maps_daily_loss_limit_error_code() -> None:
    response = trade(
        TradeRequest(symbol="XAUUSD_DAILY_LOSS", volume=0.1, side="buy"),
        service=_DispatchService(),
    )

    assert response.success is False
    assert response.error["code"] == "daily_loss_limit"


def test_trade_from_signal_is_executed_by_trade_module_api() -> None:
    service = _DispatchService()
    response = trade_from_signal(
        SignalExecuteTradeRequest(signal_id="sig_1"),
        signal_service=_SignalService(),
        command_service=service,
        query_service=service,
    )

    assert response.success is True
    assert response.metadata["operation"] == "trade_from_signal"
    assert response.data["ticket"] == 1
    assert service.last_dispatch[1]["request_id"] == "sig_1"
    assert service.last_dispatch[1]["metadata"]["entry_origin"] == "auto"
    assert service.last_dispatch[1]["metadata"]["signal"]["timeframe"] == "M5"


def test_trade_from_signal_maps_risk_blocked_error() -> None:
    class _BlockedDispatchService(_DispatchService):
        def dispatch_operation(self, operation, payload):
            self.last_dispatch = (operation, payload)
            raise PreTradeRiskBlockedError(
                "blocked by risk",
                assessment={
                    "verdict": "block",
                    "checks": [{"name": "daily_loss_limit"}],
                },
            )

    service = _BlockedDispatchService()
    response = trade_from_signal(
        SignalExecuteTradeRequest(signal_id="sig_1"),
        signal_service=_SignalService(),
        command_service=service,
        query_service=service,
    )

    assert response.success is False
    assert response.error["code"] == "daily_loss_limit"
    assert response.error["details"]["account_alias"] == "live"
    assert response.error["details"]["symbol"] == "XAUUSD"


def test_trade_from_signal_maps_mt5_trade_error() -> None:
    class _FailingDispatchService(_DispatchService):
        def dispatch_operation(self, operation, payload):
            self.last_dispatch = (operation, payload)
            raise MT5TradeError("market closed")

    service = _FailingDispatchService()
    response = trade_from_signal(
        SignalExecuteTradeRequest(signal_id="sig_1"),
        signal_service=_SignalService(),
        command_service=service,
        query_service=service,
    )

    assert response.success is False
    assert response.error["code"] == "market_closed"
    assert response.error["details"]["account_alias"] == "live"
    assert response.error["details"]["symbol"] == "XAUUSD"


def test_trade_batch_maps_mt5_error() -> None:
    response = trade_batch(
        BatchTradeRequest(
            trades=[TradeRequest(symbol="XAUUSD_BATCH_FAIL", volume=0.1, side="buy")],
            stop_on_error=True,
        ),
        service=_DispatchService(),
    )

    assert response.success is False
    assert response.error["code"] == "market_closed"
    assert response.error["details"]["count"] == 1


def test_estimate_margin_maps_mt5_error() -> None:
    response = estimate_margin(
        EstimateMarginRequest(symbol="XAUUSD_MARGIN_FAIL", volume=0.1, side="buy"),
        service=_DispatchService(),
    )

    assert response.success is False
    assert response.error["code"] == "market_closed"
    assert response.error["details"]["symbol"] == "XAUUSD_MARGIN_FAIL"


def test_modify_orders_maps_order_not_found() -> None:
    response = modify_orders(
        ModifyOrdersRequest(symbol="XAUUSD_ORDER_FAIL", magic=7, sl=1.0, tp=2.0),
        service=_DispatchService(),
    )

    assert response.success is False
    assert response.error["code"] == "order_not_found"
    assert response.error["details"]["magic"] == 7


def test_modify_positions_forwards_ticket_and_maps_position_not_found() -> None:
    service = _DispatchService()
    response = modify_positions(
        ModifyPositionsRequest(ticket=404, symbol="XAUUSD", magic=7, sl=1.0, tp=2.0),
        service=service,
    )

    assert service.last_modify_positions["ticket"] == 404
    assert response.success is False
    assert response.error["code"] == "position_not_found"
    assert response.error["details"]["ticket"] == 404


def test_positions_endpoint_serializes_dataclass_time_without_duplicate_keyword() -> (
    None
):
    response = positions(symbol="XAUUSD", magic=7, service=_DispatchService())

    assert response.success is True
    assert response.data[0].time == "2026-01-01T00:00:00+00:00"


def test_orders_endpoint_serializes_dataclass_time_without_duplicate_keyword() -> None:
    response = orders(symbol="XAUUSD", magic=7, service=_DispatchService())

    assert response.success is True
    assert response.data[0].time == "2026-01-01T00:00:00+00:00"


# ── 废弃端点 metadata 标记 ────────────────────────────────────────


def test_positions_endpoint_metadata_marks_deprecation_with_workbench_successor() -> (
    None
):
    response = positions(symbol="XAUUSD", magic=7, service=_DispatchService())

    assert response.success is True
    assert response.metadata.get("deprecated") is True
    deprecation = response.metadata.get("deprecation") or {}
    assert deprecation.get("successor") == "/v1/execution/workbench"
    assert deprecation.get("successor_query") == "include=positions"
    assert deprecation.get("sunset") == "2026-06-01"
    assert deprecation.get("reason") == "duplicate_of_workbench_block"


def test_orders_endpoint_metadata_marks_deprecation_with_workbench_successor() -> None:
    response = orders(symbol="XAUUSD", magic=7, service=_DispatchService())

    assert response.success is True
    assert response.metadata.get("deprecated") is True
    deprecation = response.metadata.get("deprecation") or {}
    assert deprecation.get("successor") == "/v1/execution/workbench"
    assert deprecation.get("successor_query") == "include=orders"


def test_positions_endpoint_marked_deprecated_in_openapi() -> None:
    """FastAPI deprecated=True → OpenAPI 文档可让前端 codegen 自动告警。"""
    from src.api import app

    schema = app.openapi()
    op = schema["paths"]["/v1/positions"]["get"]
    assert op.get("deprecated") is True


def test_orders_endpoint_marked_deprecated_in_openapi() -> None:
    from src.api import app

    schema = app.openapi()
    op = schema["paths"]["/v1/orders"]["get"]
    assert op.get("deprecated") is True
