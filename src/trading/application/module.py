from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, Optional
from uuid import uuid4

from src.persistence.db import TimescaleWriter
from src.config import get_trading_config, get_trading_ops_config
from src.risk.service import PreTradeRiskBlockedError
from src.signals.metadata_keys import MetadataKey as MK

from .services import TradingCommandService, TradingQueryService
from .control import TradeControlStateService
from ..models import TradeCommandAuditRecord
from .audit import TradeCommandAuditService, TradeDailyStatsService
from .idempotency import (
    TradeExecutionReplayService,
    TradeOperatorActionReplayService,
)
from ..registry import TradingAccountRegistry

logger = logging.getLogger(__name__)


PRECHECK_TRADE_FIELDS = {
    "symbol",
    "volume",
    "side",
    "order_kind",
    "price",
    "sl",
    "tp",
    "deviation",
    "comment",
    "magic",
    "metadata",
}

_IDEMPOTENT_LOOKBACK_LIMIT = 200
_OPERATOR_ACTION_OPERATION_TYPES = {
    "close_position",
    "close_all_positions",
    "close_positions_by_tickets",
    "cancel_orders",
    "cancel_orders_by_tickets",
}


class TradingModule:
    def __init__(
        self,
        registry: TradingAccountRegistry,
        db_writer: Optional[TimescaleWriter] = None,
        active_account_alias: Optional[str] = None,
    ):
        self.registry = registry
        self.db_writer = db_writer
        self.active_account_alias = self.registry.resolve_alias(active_account_alias)
        self._trade_control = TradeControlStateService()
        self._command_audit = TradeCommandAuditService(
            db_writer=self.db_writer,
            account_alias_getter=lambda: self.active_account_alias,
            account_key_getter=lambda: self.active_account_key,
        )
        self._daily_stats = TradeDailyStatsService()
        self._trade_execution_replay = TradeExecutionReplayService(
            self._command_audit,
            audit_lookback_limit=_IDEMPOTENT_LOOKBACK_LIMIT,
        )
        self._operator_action_replay = TradeOperatorActionReplayService(
            self._command_audit,
            audit_lookback_limit=_IDEMPOTENT_LOOKBACK_LIMIT,
        )
        self.commands = TradingCommandService(self)
        self.queries = TradingQueryService(self)

    def _active_account(self) -> str:
        return self.active_account_alias

    @property
    def active_account_key(self) -> str:
        profile = self._active_account_profile()
        account_key = profile.get("account_key")
        return str(account_key or "")

    def _account_key_for(self, account_alias: Optional[str]) -> str:
        alias = self.registry.resolve_alias(account_alias or self.active_account_alias)
        for item in self.registry.list_accounts():
            if item.get("alias") == alias:
                account_key = item.get("account_key")
                return str(account_key or "")
        if alias == self.active_account_alias:
            account_key = self._active_account_profile().get("account_key")
            return str(account_key or "")
        return ""

    def _active_scope(self):
        return self.registry.operation_scope(self._active_account())

    def _active_account_profile(self) -> dict:
        for item in self.registry.list_accounts():
            if item.get("alias") == self.active_account_alias:
                enriched = dict(item)
                enriched["active"] = True
                return enriched
        return {
            "alias": self.active_account_alias,
            "label": self.active_account_alias,
            "account_key": "",
            "login": None,
            "server": None,
            "environment": None,
            "timezone": "UTC",
            "enabled": True,
            "default": True,
            "active": True,
        }

    def _record_command_audit(self, record: TradeCommandAuditRecord) -> None:
        try:
            self._command_audit.record(record)
        except Exception:
            logger.exception("Failed to persist trade command audit for %s", record.command_type)

    def _cache_successful_trade_result(
        self,
        request_id: Optional[str],
        result: Any,
    ) -> None:
        self._trade_execution_replay.cache_successful_trade_result(request_id, result)

    def _find_idempotent_trade_result(self, request_id: str) -> Optional[dict[str, Any]]:
        return self._trade_execution_replay.find_successful_trade_result(request_id)

    def find_operator_action_replay(
        self,
        *,
        command_type: str,
        idempotency_key: Optional[str],
        request_payload: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        return self._operator_action_replay.find_recorded_action(
            command_type=command_type,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
        )

    def trade_control_status(self) -> dict[str, Any]:
        return self._trade_control.status()

    def set_trade_control_update_hook(
        self,
        fn: Optional[Callable[[dict[str, Any]], None]],
    ) -> None:
        self._trade_control.set_update_hook(fn)

    def apply_trade_control_state(self, state: dict[str, Any]) -> dict[str, Any]:
        return self._trade_control.apply_state(state)

    def update_trade_control(
        self,
        *,
        auto_entry_enabled: Optional[bool] = None,
        close_only_mode: Optional[bool] = None,
        reason: Optional[str] = None,
        actor: Optional[str] = None,
        action_id: Optional[str] = None,
        audit_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        request_context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return self._trade_control.update(
            auto_entry_enabled=auto_entry_enabled,
            close_only_mode=close_only_mode,
            reason=reason,
            actor=actor,
            action_id=action_id,
            audit_id=audit_id,
            idempotency_key=idempotency_key,
            request_context=request_context,
        )

    def record_operator_action(
        self,
        *,
        command_type: str,
        request_payload: dict[str, Any],
        response_payload: dict[str, Any],
        status: str = "success",
        error_message: Optional[str] = None,
        operation_id: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> dict[str, Any]:
        normalized_request_payload = dict(request_payload or {})
        normalized_response_payload = dict(response_payload or {})
        resolved_operation_id = operation_id or uuid4().hex
        recorded_at = datetime.now(timezone.utc)
        normalized_request_payload.setdefault("action_id", resolved_operation_id)
        if not normalized_response_payload.get("action_id"):
            normalized_response_payload["action_id"] = resolved_operation_id
        if not normalized_response_payload.get("audit_id"):
            normalized_response_payload["audit_id"] = resolved_operation_id
        if not normalized_response_payload.get("recorded_at"):
            normalized_response_payload["recorded_at"] = recorded_at.isoformat()
        details_payload = normalized_response_payload.get("details")
        if isinstance(details_payload, dict):
            if not details_payload.get("action_id"):
                details_payload["action_id"] = resolved_operation_id
            if not details_payload.get("audit_id"):
                details_payload["audit_id"] = resolved_operation_id
            if not details_payload.get("recorded_at"):
                details_payload["recorded_at"] = recorded_at.isoformat()
        record = TradeCommandAuditRecord(
            account_alias=self.active_account_alias,
            account_key=self._account_key_for(self.active_account_alias),
            command_type=command_type,
            status=status,
            symbol=symbol or normalized_request_payload.get("symbol"),
            error_message=error_message,
            request_payload=normalized_request_payload,
            response_payload=normalized_response_payload,
            recorded_at=recorded_at,
            operation_id=resolved_operation_id,
        )
        self._record_command_audit(record)
        self._operator_action_replay.cache_recorded_action(
            command_type=command_type,
            request_payload=normalized_request_payload,
            response_payload=normalized_response_payload,
        )
        logger.info(
            "Operator action recorded: account=%s command=%s status=%s action_id=%s actor=%s reason=%s idempotency_key=%s",
            self.active_account_alias,
            command_type,
            status,
            resolved_operation_id,
            normalized_request_payload.get("actor"),
            normalized_request_payload.get("reason"),
            normalized_request_payload.get("idempotency_key"),
        )
        return {
            "operation_id": record.operation_id,
            "recorded_at": record.recorded_at.isoformat(),
            "status": record.status,
        }

    @staticmethod
    def _operator_action_requested(
        operation_type: str,
        payload: dict[str, Any],
    ) -> bool:
        if operation_type not in _OPERATOR_ACTION_OPERATION_TYPES:
            return False
        if str(payload.get("action_id") or "").strip():
            return True
        if str(payload.get("idempotency_key") or "").strip():
            return True
        if str(payload.get("actor") or "").strip():
            return True
        return False

    def _build_operator_action_response(
        self,
        *,
        operation_type: str,
        payload: dict[str, Any],
        raw_result: Any,
        recorded_at: datetime,
        audit_id: str,
        error_message: Optional[str] = None,
    ) -> tuple[dict[str, Any], str]:
        action_id = str(payload.get("action_id") or audit_id).strip() or audit_id
        actor = str(payload.get("actor") or "").strip() or None
        reason = str(payload.get("reason") or "").strip() or None
        idempotency_key = str(payload.get("idempotency_key") or "").strip() or None
        request_context = (
            dict(payload.get("request_context"))
            if isinstance(payload.get("request_context"), dict)
            else {}
        )
        base_payload = {
            "accepted": error_message is None,
            "status": "failed",
            "action_id": action_id,
            "audit_id": audit_id,
            "actor": actor,
            "reason": reason,
            "idempotency_key": idempotency_key,
            "request_context": request_context,
            "message": None,
            "error_code": None,
            "recorded_at": recorded_at.isoformat(),
            "effective_state": {},
            "result": raw_result if isinstance(raw_result, dict) else raw_result,
        }
        if error_message is not None:
            base_payload["message"] = error_message
            base_payload["error_message"] = error_message
            base_payload["details"] = {"operation": operation_type}
            return base_payload, "failed"

        if operation_type == "close_position":
            result_payload = dict(raw_result or {}) if isinstance(raw_result, dict) else {}
            success = bool(result_payload.get("success", True)) and raw_result is not None
            base_payload["accepted"] = success
            base_payload["status"] = "completed" if success else "failed"
            base_payload["message"] = (
                "position close completed"
                if success
                else "position close reported failure"
            )
            base_payload["effective_state"] = {"result": result_payload}
            if not success:
                base_payload["error_message"] = base_payload["message"]
                base_payload["details"] = {"operation": operation_type}
                return base_payload, "failed"
            return base_payload, "success"

        result_payload = dict(raw_result or {}) if isinstance(raw_result, dict) else {}
        success_key = "closed" if operation_type in {
            "close_all_positions",
            "close_positions_by_tickets",
        } else "canceled"
        success_items = list(result_payload.get(success_key) or [])
        failed_items = list(result_payload.get("failed") or [])
        if failed_items and success_items:
            status = "partial_failure"
            audit_status = "partial_failure"
        elif failed_items:
            status = "failed"
            audit_status = "failed"
        else:
            status = "completed"
            audit_status = "success"
        label = {
            "close_all_positions": "close all positions",
            "close_positions_by_tickets": "batch close",
            "cancel_orders": "cancel orders",
            "cancel_orders_by_tickets": "batch cancel orders",
        }.get(operation_type, operation_type)
        message = {
            "completed": f"{label} completed",
            "partial_failure": f"{label} finished with partial failure",
            "failed": f"{label} failed",
        }[status]
        base_payload["accepted"] = status != "failed"
        base_payload["status"] = status
        base_payload["message"] = message
        base_payload["effective_state"] = {"result": result_payload}
        if status == "failed":
            base_payload["error_message"] = message
            base_payload["details"] = {"operation": operation_type}
        return base_payload, audit_status

    def _enforce_trade_control(self, payload: Dict[str, Any]) -> None:
        self._trade_control.enforce(payload)

    def _update_daily_stats(self, record: TradeCommandAuditRecord) -> None:
        self._daily_stats.update(record)

    def _run_trade_with_dispatch_controls(self, payload: Dict[str, Any]) -> dict:
        config = get_trading_ops_config()
        self._enforce_trade_control(payload)
        required = ("symbol", "volume", "side")
        missing = [key for key in required if payload.get(key) in (None, "")]
        if missing:
            raise ValueError(f"trade payload missing required fields: {', '.join(missing)}")
        try:
            volume = float(payload.get("volume"))
        except Exception as exc:  # noqa: BLE001
            raise ValueError("trade payload volume must be numeric") from exc
        if volume <= 0:
            raise ValueError("trade payload volume must be > 0")

        precheck_payload = {key: value for key, value in payload.items() if key in PRECHECK_TRADE_FIELDS}
        precheck = self.precheck_trade(**precheck_payload)
        action = str(precheck.get("verdict") or "allow").lower()
        if config.dispatch_strict_mode and action == "block":
            raise PreTradeRiskBlockedError(
                precheck.get("reason") or "trade blocked by risk control",
                assessment=precheck,
            )
        result = self.execute_trade(**payload)
        if isinstance(result, dict):
            result.setdefault("dispatch_precheck", precheck)
        return result

    def _execute_command(
        self,
        operation_type: str,
        account_alias: Optional[str],
        payload: Dict[str, Any],
        fn,
        *,
        operation_id: Optional[str] = None,
    ):
        started = time.monotonic()
        resolved_alias = self.registry.resolve_alias(account_alias)
        trace_id = str(payload.get("request_id") or payload.get("trace_id") or "")
        operator_action_requested = self._operator_action_requested(operation_type, payload)
        resolved_operation_id = str(operation_id or payload.get("action_id") or "").strip() or None
        if operator_action_requested and not resolved_operation_id:
            resolved_operation_id = uuid4().hex
        try:
            raw_result = fn()
            record_status = "success"
            result = raw_result
            if operator_action_requested:
                result, record_status = self._build_operator_action_response(
                    operation_type=operation_type,
                    payload=payload,
                    raw_result=raw_result,
                    recorded_at=datetime.now(timezone.utc),
                    audit_id=resolved_operation_id,
                )
            if isinstance(result, dict):
                if not trace_id:
                    trace_id = str(result.get("request_id") or "")
                if not trace_id and isinstance(raw_result, dict):
                    trace_id = str(raw_result.get("request_id") or "")
                if not trace_id:
                    trace_id = f"{operation_type}_{int(started * 1000)}"
                result.setdefault("trace_id", trace_id)
                result.setdefault("account_alias", resolved_alias)
                if operation_type == "execute_trade":
                    self._cache_successful_trade_result(
                        str(payload.get("request_id") or result.get("request_id") or ""),
                        result,
                    )
            duration_ms = int((time.monotonic() - started) * 1000)
            record = TradeCommandAuditRecord(
                account_alias=resolved_alias,
                account_key=self._account_key_for(resolved_alias),
                command_type=operation_type,
                status=record_status,
                symbol=payload.get("symbol"),
                side=payload.get("side"),
                order_kind=payload.get("order_kind"),
                volume=payload.get("volume"),
                ticket=payload.get("ticket"),
                magic=payload.get("magic"),
                duration_ms=duration_ms,
                request_payload=payload,
                response_payload=result if isinstance(result, dict) else {"result": result},
                operation_id=resolved_operation_id or uuid4().hex,
            )
            if isinstance(result, dict):
                result.setdefault("operation_id", record.operation_id)
            self._record_command_audit(record)
            if operator_action_requested and isinstance(result, dict):
                self._operator_action_replay.cache_recorded_action(
                    command_type=operation_type,
                    request_payload=payload,
                    response_payload=result,
                )
            self._update_daily_stats(record)
            return result
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            response_payload: dict[str, Any] = {}
            if operator_action_requested:
                response_payload, _ = self._build_operator_action_response(
                    operation_type=operation_type,
                    payload=payload,
                    raw_result=None,
                    recorded_at=datetime.now(timezone.utc),
                    audit_id=resolved_operation_id or uuid4().hex,
                    error_message=str(exc),
                )
            record = TradeCommandAuditRecord(
                account_alias=resolved_alias,
                account_key=self._account_key_for(resolved_alias),
                command_type=operation_type,
                status="failed",
                symbol=payload.get("symbol"),
                side=payload.get("side"),
                order_kind=payload.get("order_kind"),
                volume=payload.get("volume"),
                ticket=payload.get("ticket"),
                magic=payload.get("magic"),
                duration_ms=duration_ms,
                error_message=str(exc),
                request_payload={**payload, "trace_id": trace_id or None},
                response_payload=response_payload,
                operation_id=resolved_operation_id or uuid4().hex,
            )
            self._record_command_audit(record)
            if operator_action_requested and response_payload:
                self._operator_action_replay.cache_recorded_action(
                    command_type=operation_type,
                    request_payload=payload,
                    response_payload=response_payload,
                )
            self._update_daily_stats(record)
            raise

    def dispatch_operation(self, operation: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        payload = payload or {}
        handlers = {
            "trade": lambda: self._run_trade_with_dispatch_controls(payload),
            "trade_precheck": lambda: self.precheck_trade(**payload),
            "close": lambda: self.close_position(**payload),
            "close_all": lambda: self.close_all_positions(**payload),
            "cancel_orders": lambda: self.cancel_orders(**payload),
        }
        if operation not in handlers:
            raise ValueError(f"unsupported trading operation: {operation}")
        return handlers[operation]()

    def daily_trade_summary(self, summary_date: Optional[date] = None) -> dict[str, Any]:
        return self._daily_stats.summary(
            account_alias=self.active_account_alias,
            summary_date=summary_date,
        )

    def entry_to_order_status(
        self,
        symbol: Optional[str] = None,
        volume: float = 0.1,
        side: str = "buy",
        order_kind: str = "market",
    ) -> dict[str, Any]:
        account_alias = self.active_account_alias
        health = self.health()
        account_ready = False
        risk_action = "allow"
        risk_reason = None
        try:
            account = self.account_info()
            account_ready = account is not None
        except Exception as exc:  # noqa: BLE001
            account_ready = False
            risk_reason = f"account info unavailable: {exc}"

        target_symbol = symbol or get_trading_config().default_symbol
        try:
            precheck = self.precheck_trade(
                symbol=target_symbol,
                volume=volume,
                side=side,
                order_kind=order_kind,
            )
            risk_action = str(precheck.get("verdict") or "allow")
            if precheck.get("reason"):
                risk_reason = precheck.get("reason")
        except Exception as exc:  # noqa: BLE001
            precheck = {"verdict": "warn", "reason": str(exc)}
            risk_action = "warn"
            risk_reason = str(exc)

        stage_status = {
            "entry": "ready",
            "connection": "ready" if health.get("connected", False) else "failed",
            "account": "ready" if account_ready else "failed",
            "risk": "ready" if risk_action != "block" else "blocked",
            "order": "ready" if health.get("connected", False) and account_ready and risk_action != "block" else "blocked",
        }
        return {
            "account_alias": account_alias,
            "symbol": target_symbol,
            "health": health,
            "precheck": precheck,
            "risk_action": risk_action,
            "risk_reason": risk_reason,
            "stages": stage_status,
            "ready_for_order": stage_status["order"] == "ready",
        }

    def list_accounts(self) -> list[dict]:
        return [self._active_account_profile()]

    def health(self) -> dict[str, Any]:
        try:
            with self._active_scope() as (
                alias,
                trading_service,
                _account_service,
            ):
                status = trading_service.health()
                if not isinstance(status, dict):
                    status = {"status": str(status)}
                return {
                    "account_alias": alias,
                    **status,
                }
        except Exception as exc:
            return {
                "account_alias": self.active_account_alias,
                "connected": False,
                "error": str(exc),
            }

    def account_info(self):
        with self._active_scope() as (
            alias,
            _trading,
            account_service,
        ):
            return account_service.account_info()

    def positions(self, symbol: Optional[str] = None):
        with self._active_scope() as (
            alias,
            _trading,
            account_service,
        ):
            return account_service.positions(symbol)

    def orders(self, symbol: Optional[str] = None):
        with self._active_scope() as (
            alias,
            _trading,
            account_service,
        ):
            return account_service.orders(symbol)

    def execute_trade(self, **kwargs: Any):
        request_id = str(kwargs.get("request_id") or "").strip()
        if request_id:
            replayed = self._find_idempotent_trade_result(request_id)
            if replayed is not None:
                return replayed
        self._enforce_trade_control(kwargs)
        with self._active_scope() as (
            alias,
            trading_service,
            _account_service,
        ):
            payload = {"account_alias": alias, **kwargs}
            return self._execute_command("execute_trade", alias, payload, lambda: trading_service.execute_trade(**kwargs))

    def precheck_trade(self, **kwargs: Any):
        with self._active_scope() as (
            alias,
            trading_service,
            _account_service,
        ):
            payload = {"account_alias": alias, **kwargs}
            return self._execute_command("precheck_trade", alias, payload, lambda: trading_service.precheck_trade(**kwargs))

    def execute_trade_batch(self, trades: list[dict], stop_on_error: bool = False):
        all_results = []
        success_count = 0
        failure_count = 0
        for index, trade in enumerate(trades):
            alias = self._active_account()
            payload = dict(trade)
            try:
                result = self.execute_trade(**payload)
                all_results.append({"index": index, "success": True, "result": result, "account_alias": alias})
                success_count += 1
            except Exception as exc:
                all_results.append({"index": index, "success": False, "error": str(exc), "trade": dict(payload), "account_alias": alias})
                failure_count += 1
                if stop_on_error:
                    break

        batch_alias = self.active_account_alias
        self._record_command_audit(
            TradeCommandAuditRecord(
                account_alias=batch_alias,
                account_key=self._account_key_for(batch_alias),
                command_type="execute_trade_batch",
                status="success" if failure_count == 0 else "partial_failure",
                duration_ms=None,
                request_payload={"count": len(trades), "stop_on_error": stop_on_error},
                response_payload={
                    "success_count": success_count,
                    "failure_count": failure_count,
                    "results": all_results,
                },
            )
        )
        return {
            "results": all_results,
            "success_count": success_count,
            "failure_count": failure_count,
            "stop_on_error": stop_on_error,
        }

    def close_position(self, **kwargs: Any):
        operator_kwargs = {
            "actor": kwargs.pop("actor", None),
            "reason": kwargs.pop("reason", None),
            "action_id": kwargs.pop("action_id", None),
            "audit_id": kwargs.pop("audit_id", None),
            "idempotency_key": kwargs.pop("idempotency_key", None),
            "request_context": kwargs.pop("request_context", None),
        }
        with self._active_scope() as (
            alias,
            trading_service,
            _account_service,
        ):
            payload = {
                "account_alias": alias,
                **kwargs,
                **operator_kwargs,
            }
            return self._execute_command(
                "close_position",
                alias,
                payload,
                lambda: trading_service.close_position(**kwargs),
                operation_id=operator_kwargs.get("action_id"),
            )

    def close_all_positions(self, **kwargs: Any):
        operator_kwargs = {
            "actor": kwargs.pop("actor", None),
            "reason": kwargs.pop("reason", None),
            "action_id": kwargs.pop("action_id", None),
            "audit_id": kwargs.pop("audit_id", None),
            "idempotency_key": kwargs.pop("idempotency_key", None),
            "request_context": kwargs.pop("request_context", None),
        }
        with self._active_scope() as (
            alias,
            trading_service,
            _account_service,
        ):
            payload = {
                "account_alias": alias,
                **kwargs,
                **operator_kwargs,
            }
            return self._execute_command(
                "close_all_positions",
                alias,
                payload,
                lambda: trading_service.close_all_positions(**kwargs),
                operation_id=operator_kwargs.get("action_id"),
            )

    def close_positions_by_tickets(
        self,
        tickets: list[int],
        deviation: int = 20,
        comment: str = "close_batch",
        *,
        actor: Optional[str] = None,
        reason: Optional[str] = None,
        action_id: Optional[str] = None,
        audit_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        request_context: Optional[dict[str, Any]] = None,
    ):
        with self._active_scope() as (
            alias,
            trading_service,
            _account_service,
        ):
            payload = {
                "account_alias": alias,
                "tickets": tickets,
                "deviation": deviation,
                "comment": comment,
                "actor": actor,
                "reason": reason,
                "action_id": action_id,
                "audit_id": audit_id,
                "idempotency_key": idempotency_key,
                "request_context": request_context,
            }
            return self._execute_command(
                "close_positions_by_tickets",
                alias,
                payload,
                lambda: trading_service.close_positions_by_tickets(tickets, deviation=deviation, comment=comment),
                operation_id=action_id,
            )

    def cancel_orders(self, **kwargs: Any):
        operator_kwargs = {
            "actor": kwargs.pop("actor", None),
            "reason": kwargs.pop("reason", None),
            "action_id": kwargs.pop("action_id", None),
            "audit_id": kwargs.pop("audit_id", None),
            "idempotency_key": kwargs.pop("idempotency_key", None),
            "request_context": kwargs.pop("request_context", None),
        }
        with self._active_scope() as (
            alias,
            trading_service,
            _account_service,
        ):
            payload = {
                "account_alias": alias,
                **kwargs,
                **operator_kwargs,
            }
            return self._execute_command(
                "cancel_orders",
                alias,
                payload,
                lambda: trading_service.cancel_orders(**kwargs),
                operation_id=operator_kwargs.get("action_id"),
            )

    def cancel_orders_by_tickets(
        self,
        tickets: list[int],
        *,
        actor: Optional[str] = None,
        reason: Optional[str] = None,
        action_id: Optional[str] = None,
        audit_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        request_context: Optional[dict[str, Any]] = None,
    ):
        with self._active_scope() as (
            alias,
            trading_service,
            _account_service,
        ):
            payload = {
                "account_alias": alias,
                "tickets": tickets,
                "actor": actor,
                "reason": reason,
                "action_id": action_id,
                "audit_id": audit_id,
                "idempotency_key": idempotency_key,
                "request_context": request_context,
            }
            return self._execute_command(
                "cancel_orders_by_tickets",
                alias,
                payload,
                lambda: trading_service.cancel_orders_by_tickets(tickets),
                operation_id=action_id,
            )

    def estimate_margin(self, **kwargs: Any):
        with self._active_scope() as (
            _alias,
            trading_service,
            _account_service,
        ):
            return {"margin": trading_service.estimate_margin(**kwargs)}

    def modify_orders(self, **kwargs: Any):
        with self._active_scope() as (
            alias,
            trading_service,
            _account_service,
        ):
            payload = {"account_alias": alias, **kwargs}
            return self._execute_command("modify_orders", alias, payload, lambda: trading_service.modify_orders(**kwargs))

    def modify_positions(self, **kwargs: Any):
        with self._active_scope() as (
            alias,
            trading_service,
            _account_service,
        ):
            payload = {"account_alias": alias, **kwargs}
            return self._execute_command("modify_positions", alias, payload, lambda: trading_service.modify_positions(**kwargs))

    def get_positions(self, symbol: Optional[str] = None, magic: Optional[int] = None):
        with self._active_scope() as (
            _alias,
            trading_service,
            _account_service,
        ):
            return trading_service.get_positions(symbol, magic)

    def get_orders(self, symbol: Optional[str] = None, magic: Optional[int] = None):
        with self._active_scope() as (
            _alias,
            trading_service,
            _account_service,
        ):
            return trading_service.get_orders(symbol, magic)

    def get_position_close_details(
        self,
        ticket: int,
        *,
        symbol: Optional[str] = None,
        lookback_days: int = 7,
    ) -> Optional[dict[str, Any]]:
        with self._active_scope() as (
            _alias,
            trading_service,
            _account_service,
        ):
            return trading_service.get_position_close_details(
                ticket=ticket,
                symbol=symbol,
                lookback_days=lookback_days,
            )

    def resolve_position_context(
        self,
        *,
        ticket: int,
        comment: Optional[str] = None,
        limit: int = 500,
    ) -> Optional[dict[str, Any]]:
        for row in self.recent_command_audits(
            command_type="execute_trade",
            status="success",
            limit=limit,
        ):
            response_payload = row.get("response_payload") or {}
            request_payload = row.get("request_payload") or {}
            payload_comment = str(request_payload.get("comment") or "").strip()
            response_ticket = int(response_payload.get("ticket") or 0)
            if response_ticket != int(ticket) and (not comment or payload_comment != str(comment).strip()):
                continue
            metadata = request_payload.get("metadata") or {}
            signal_meta = metadata.get(MK.SIGNAL) or {}
            request_id = str(request_payload.get("request_id") or "").strip()
            if not request_id:
                request_id = str(signal_meta.get("signal_id") or f"restored:{ticket}")
            return {
                "signal_id": request_id,
                "timeframe": str(signal_meta.get("timeframe") or ""),
                "strategy": str(signal_meta.get("strategy") or ""),
                "confidence": signal_meta.get("confidence"),
                "regime": metadata.get(MK.REGIME),
                "fill_price": response_payload.get("fill_price") or response_payload.get("price"),
                "comment": payload_comment or comment,
                "source": "restored_signal_trade" if signal_meta else "restored_trade",
                "entry_origin": self._trade_control.entry_origin(request_payload),
                "request_id": request_id,
            }
        return None

    def recent_command_audits(
        self,
        *,
        command_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        if self.db_writer is None:
            return []
        return self._command_audit.recent_command_audits(
            command_type=command_type,
            status=status,
            limit=limit,
        )

    def command_audit_page(
        self,
        *,
        command_type: Optional[str] = None,
        status: Optional[str] = None,
        symbol: Optional[str] = None,
        signal_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        actor: Optional[str] = None,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        page: int = 1,
        page_size: int = 100,
        sort: str = "recorded_at_desc",
    ) -> dict[str, Any]:
        return self._command_audit.command_audit_page(
            command_type=command_type,
            status=status,
            symbol=symbol,
            signal_id=signal_id,
            trace_id=trace_id,
            actor=actor,
            from_time=from_time,
            to_time=to_time,
            page=page,
            page_size=page_size,
            sort=sort,
        )

    def monitoring_summary(self, *, hours: int = 24) -> dict:
        if self.db_writer is None:
            return {
                "active_account_alias": self.active_account_alias,
                "accounts": self.list_accounts(),
                "trade_control": self.trade_control_status(),
                "summary": [],
                "recent": [],
            }
        return {
            "active_account_alias": self.active_account_alias,
            "accounts": self.list_accounts(),
            "trade_control": self.trade_control_status(),
            "daily": self.daily_trade_summary(),
            "summary": self._command_audit.summarize_operations(hours=hours),
            "recent": self.recent_command_audits(limit=20),
        }
