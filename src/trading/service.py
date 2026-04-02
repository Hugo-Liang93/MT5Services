from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from threading import RLock
from typing import Any, Callable, Dict, Optional

from src.persistence.db import TimescaleWriter
from src.config import get_trading_config, get_trading_ops_config
from src.risk.service import PreTradeRiskBlockedError

from .models import TradeOperationRecord
from .registry import TradingAccountRegistry

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


class TradingModule:
    def __init__(
        self,
        registry: TradingAccountRegistry,
        db_writer: Optional[TimescaleWriter] = None,
        active_account_alias: Optional[str] = None,
    ):
        self.registry = registry
        self.db_writer = db_writer
        self.active_account_alias = self.registry.resolve_alias(
            active_account_alias or self.registry.default_account_alias()
        )
        self._daily_stats_lock = RLock()
        self._daily_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {
            "total": 0,
            "success": 0,
            "failed": 0,
            "symbols": {},
            "operations": {},
            "risk": {"blocked": 0, "warn": 0, "allow": 0},
            "last_trade_at": None,
        })
        self._trade_control_lock = RLock()
        self._trade_control_state: dict[str, Any] = {
            "auto_entry_enabled": True,
            "close_only_mode": False,
            "updated_at": None,
            "reason": None,
        }
        self._trade_control_update_hook: Optional[Callable[[dict[str, Any]], None]] = None
        self._idempotency_lock = RLock()
        self._idempotent_success_cache: dict[str, dict[str, Any]] = {}

    def _active_account(self) -> str:
        return self.active_account_alias

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
            "login": None,
            "server": None,
            "timezone": "UTC",
            "enabled": True,
            "default": True,
            "active": True,
        }

    def _json_safe(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, datetime):
            return value.isoformat()
        if is_dataclass(value):
            return self._json_safe(asdict(value))
        if isinstance(value, dict):
            return {str(k): self._json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._json_safe(item) for item in value]
        return str(value)

    def _record_operation(self, record: TradeOperationRecord) -> None:
        if self.db_writer is None:
            return
        try:
            normalized = TradeOperationRecord(
                account_alias=record.account_alias,
                operation_type=record.operation_type,
                status=record.status,
                symbol=record.symbol,
                side=record.side,
                order_kind=record.order_kind,
                volume=record.volume,
                ticket=record.ticket,
                order_id=record.order_id,
                deal_id=record.deal_id,
                magic=record.magic,
                duration_ms=record.duration_ms,
                error_message=record.error_message,
                request_payload=self._json_safe(record.request_payload),
                response_payload=self._json_safe(record.response_payload),
                recorded_at=record.recorded_at,
                operation_id=record.operation_id,
            )
            self.db_writer.write_trade_operations([normalized.to_row()])
        except Exception:
            logger.exception("Failed to persist trade operation audit for %s", record.operation_type)

    def _cache_successful_trade_result(
        self,
        request_id: Optional[str],
        result: Any,
    ) -> None:
        if not request_id or not isinstance(result, dict):
            return
        with self._idempotency_lock:
            self._idempotent_success_cache[str(request_id)] = dict(result)
            if len(self._idempotent_success_cache) > 500:
                keys = list(self._idempotent_success_cache.keys())
                for key in keys[:-250]:
                    self._idempotent_success_cache.pop(key, None)

    def _find_idempotent_trade_result(self, request_id: str) -> Optional[dict[str, Any]]:
        normalized = str(request_id or "").strip()
        if not normalized:
            return None
        with self._idempotency_lock:
            cached = self._idempotent_success_cache.get(normalized)
        if cached is not None:
            replayed = dict(cached)
            replayed["idempotent_replay"] = True
            replayed["idempotent_source"] = "memory"
            return replayed
        if self.db_writer is None:
            return None
        rows = self.db_writer.fetch_trade_operations(
            account_alias=self.active_account_alias,
            operation_type="execute_trade",
            status="success",
            limit=_IDEMPOTENT_LOOKBACK_LIMIT,
        )
        for row in rows:
            request_payload = row[15] or {}
            response_payload = row[16] or {}
            if str(request_payload.get("request_id") or "").strip() != normalized:
                continue
            if not isinstance(response_payload, dict):
                continue
            replayed = dict(response_payload)
            replayed.setdefault("operation_id", row[1])
            replayed["idempotent_replay"] = True
            replayed["idempotent_source"] = "audit"
            self._cache_successful_trade_result(normalized, replayed)
            return replayed
        return None

    def trade_control_status(self) -> dict[str, Any]:
        with self._trade_control_lock:
            return dict(self._trade_control_state)

    def set_trade_control_update_hook(
        self,
        fn: Optional[Callable[[dict[str, Any]], None]],
    ) -> None:
        self._trade_control_update_hook = fn

    def apply_trade_control_state(self, state: dict[str, Any]) -> dict[str, Any]:
        with self._trade_control_lock:
            self._trade_control_state["auto_entry_enabled"] = bool(
                state.get("auto_entry_enabled", True)
            )
            self._trade_control_state["close_only_mode"] = bool(
                state.get("close_only_mode", False)
            )
            self._trade_control_state["reason"] = str(state.get("reason") or "").strip() or None
            updated_at = state.get("updated_at")
            self._trade_control_state["updated_at"] = (
                updated_at.isoformat() if isinstance(updated_at, datetime) else updated_at
            )
            return dict(self._trade_control_state)

    def update_trade_control(
        self,
        *,
        auto_entry_enabled: Optional[bool] = None,
        close_only_mode: Optional[bool] = None,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        with self._trade_control_lock:
            if auto_entry_enabled is not None:
                self._trade_control_state["auto_entry_enabled"] = bool(auto_entry_enabled)
            if close_only_mode is not None:
                self._trade_control_state["close_only_mode"] = bool(close_only_mode)
            self._trade_control_state["reason"] = str(reason).strip() or None
            self._trade_control_state["updated_at"] = datetime.now(timezone.utc).isoformat()
            snapshot = dict(self._trade_control_state)
        if self._trade_control_update_hook is not None:
            self._trade_control_update_hook(snapshot)
        return snapshot

    @staticmethod
    def _entry_origin(payload: Dict[str, Any]) -> str:
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        if isinstance(metadata, dict):
            return str(metadata.get("entry_origin") or "manual").strip().lower()
        return "manual"

    def _build_control_assessment(
        self,
        *,
        reason: str,
        payload: Dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "enabled": True,
            "mode": "strict",
            "blocked": True,
            "verdict": "block",
            "reason": reason,
            "symbol": payload.get("symbol"),
            "active_windows": [],
            "upcoming_windows": [],
            "warnings": [],
            "checks": [
                {
                    "name": "trade_control",
                    "verdict": "block",
                    "reason": reason,
                    "details": {
                        "control_state": self.trade_control_status(),
                        "entry_origin": self._entry_origin(payload),
                    },
                }
            ],
            "intent": {k: v for k, v in payload.items() if k in PRECHECK_TRADE_FIELDS},
        }

    def _enforce_trade_control(self, payload: Dict[str, Any]) -> None:
        state = self.trade_control_status()
        entry_origin = self._entry_origin(payload)
        if state.get("close_only_mode"):
            raise PreTradeRiskBlockedError(
                "trade entry disabled: close_only_mode_enabled",
                assessment=self._build_control_assessment(
                    reason="close_only_mode_enabled",
                    payload=payload,
                ),
            )
        if entry_origin == "auto" and not bool(state.get("auto_entry_enabled", True)):
            raise PreTradeRiskBlockedError(
                "trade entry disabled: auto_entry_paused",
                assessment=self._build_control_assessment(
                    reason="auto_entry_paused",
                    payload=payload,
                ),
            )

    def _update_daily_stats(self, record: TradeOperationRecord) -> None:
        if record.operation_type not in {
            "execute_trade",
            "precheck_trade",
            "close_position",
            "close_all_positions",
            "cancel_orders",
            "modify_orders",
            "modify_positions",
        }:
            return
        day = (record.recorded_at or datetime.now(timezone.utc)).date().isoformat()
        with self._daily_stats_lock:
            bucket = self._daily_stats[day]
            bucket["total"] += 1
            if record.status == "success":
                bucket["success"] += 1
            else:
                bucket["failed"] += 1
            symbol = record.symbol or "unknown"
            symbol_stats = bucket["symbols"].setdefault(symbol, {"total": 0, "success": 0, "failed": 0})
            symbol_stats["total"] += 1
            if record.status == "success":
                symbol_stats["success"] += 1
            else:
                symbol_stats["failed"] += 1
            op_stats = bucket["operations"].setdefault(record.operation_type, {"total": 0, "success": 0, "failed": 0})
            op_stats["total"] += 1
            if record.status == "success":
                op_stats["success"] += 1
            else:
                op_stats["failed"] += 1
            if record.operation_type == "precheck_trade" and isinstance(record.response_payload, dict):
                action = str(record.response_payload.get("verdict") or "allow").lower()
                if action not in {"allow", "warn", "block"}:
                    action = "allow"
                bucket["risk"]["blocked" if action == "block" else action] += 1
            bucket["last_trade_at"] = (record.recorded_at or datetime.now(timezone.utc)).isoformat()

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

    def _execute(self, operation_type: str, account_alias: Optional[str], payload: Dict[str, Any], fn):
        started = time.monotonic()
        resolved_alias = self.registry.resolve_alias(account_alias)
        trace_id = str(payload.get("request_id") or payload.get("trace_id") or "")
        try:
            result = fn()
            if isinstance(result, dict):
                if not trace_id:
                    trace_id = str(result.get("request_id") or "")
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
            record = TradeOperationRecord(
                account_alias=resolved_alias,
                operation_type=operation_type,
                status="success",
                symbol=payload.get("symbol"),
                side=payload.get("side"),
                order_kind=payload.get("order_kind"),
                volume=payload.get("volume"),
                ticket=payload.get("ticket"),
                magic=payload.get("magic"),
                duration_ms=duration_ms,
                request_payload=payload,
                response_payload=result if isinstance(result, dict) else {"result": result},
            )
            if isinstance(result, dict):
                result.setdefault("operation_id", record.operation_id)
            self._record_operation(record)
            self._update_daily_stats(record)
            return result
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            record = TradeOperationRecord(
                account_alias=resolved_alias,
                operation_type=operation_type,
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
                response_payload={},
            )
            self._record_operation(record)
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
            "positions": lambda: self.positions(payload.get("symbol")),
            "orders": lambda: self.orders(payload.get("symbol")),
            "daily_summary": lambda: self.daily_trade_summary(),
            "entry_status": lambda: self.entry_to_order_status(**payload),
        }
        if operation not in handlers:
            raise ValueError(f"unsupported trading operation: {operation}")
        return handlers[operation]()

    def daily_trade_summary(self, summary_date: Optional[date] = None) -> dict[str, Any]:
        day_key = (summary_date or datetime.now(timezone.utc).date()).isoformat()
        with self._daily_stats_lock:
            snapshot = dict(self._daily_stats.get(day_key, {}))
        if not snapshot:
            snapshot = {
                "total": 0,
                "success": 0,
                "failed": 0,
                "symbols": {},
                "operations": {},
                "risk": {"blocked": 0, "warn": 0, "allow": 0},
                "last_trade_at": None,
            }
        total = int(snapshot.get("total", 0))
        success = int(snapshot.get("success", 0))
        return {
            "date": day_key,
            "account_alias": self.active_account_alias,
            **snapshot,
            "success_rate": round((success / total) * 100, 2) if total else 0.0,
        }

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
                client = getattr(trading_service, "client", None)
                status = client.health() if client and hasattr(client, "health") else {}
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
            return self._execute("account_info", alias, {"account_alias": alias}, account_service.account_info)

    def positions(self, symbol: Optional[str] = None):
        with self._active_scope() as (
            alias,
            _trading,
            account_service,
        ):
            return self._execute(
                "positions",
                alias,
                {"account_alias": alias, "symbol": symbol},
                lambda: account_service.positions(symbol),
            )

    def orders(self, symbol: Optional[str] = None):
        with self._active_scope() as (
            alias,
            _trading,
            account_service,
        ):
            return self._execute(
                "orders",
                alias,
                {"account_alias": alias, "symbol": symbol},
                lambda: account_service.orders(symbol),
            )

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
            return self._execute("execute_trade", alias, payload, lambda: trading_service.execute_trade(**kwargs))

    def precheck_trade(self, **kwargs: Any):
        with self._active_scope() as (
            alias,
            trading_service,
            _account_service,
        ):
            payload = {"account_alias": alias, **kwargs}
            return self._execute("precheck_trade", alias, payload, lambda: trading_service.precheck_trade(**kwargs))

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
        self._record_operation(
            TradeOperationRecord(
                account_alias=batch_alias,
                operation_type="execute_trade_batch",
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
        with self._active_scope() as (
            alias,
            trading_service,
            _account_service,
        ):
            payload = {"account_alias": alias, **kwargs}
            return self._execute("close_position", alias, payload, lambda: trading_service.close_position(**kwargs))

    def close_all_positions(self, **kwargs: Any):
        with self._active_scope() as (
            alias,
            trading_service,
            _account_service,
        ):
            payload = {"account_alias": alias, **kwargs}
            return self._execute("close_all_positions", alias, payload, lambda: trading_service.close_all_positions(**kwargs))

    def close_positions_by_tickets(self, tickets: list[int], deviation: int = 20, comment: str = "close_batch"):
        with self._active_scope() as (
            alias,
            trading_service,
            _account_service,
        ):
            payload = {"account_alias": alias, "tickets": tickets, "deviation": deviation, "comment": comment}
            return self._execute(
                "close_positions_by_tickets",
                alias,
                payload,
                lambda: trading_service.close_positions_by_tickets(tickets, deviation=deviation, comment=comment),
            )

    def cancel_orders(self, **kwargs: Any):
        with self._active_scope() as (
            alias,
            trading_service,
            _account_service,
        ):
            payload = {"account_alias": alias, **kwargs}
            return self._execute("cancel_orders", alias, payload, lambda: trading_service.cancel_orders(**kwargs))

    def cancel_orders_by_tickets(self, tickets: list[int]):
        with self._active_scope() as (
            alias,
            trading_service,
            _account_service,
        ):
            payload = {"account_alias": alias, "tickets": tickets}
            return self._execute(
                "cancel_orders_by_tickets",
                alias,
                payload,
                lambda: trading_service.cancel_orders_by_tickets(tickets),
            )

    def estimate_margin(self, **kwargs: Any):
        with self._active_scope() as (
            alias,
            trading_service,
            _account_service,
        ):
            payload = {"account_alias": alias, **kwargs}
            return self._execute("estimate_margin", alias, payload, lambda: {"margin": trading_service.estimate_margin(**kwargs)})

    def modify_orders(self, **kwargs: Any):
        with self._active_scope() as (
            alias,
            trading_service,
            _account_service,
        ):
            payload = {"account_alias": alias, **kwargs}
            return self._execute("modify_orders", alias, payload, lambda: trading_service.modify_orders(**kwargs))

    def modify_positions(self, **kwargs: Any):
        with self._active_scope() as (
            alias,
            trading_service,
            _account_service,
        ):
            payload = {"account_alias": alias, **kwargs}
            return self._execute("modify_positions", alias, payload, lambda: trading_service.modify_positions(**kwargs))

    def get_positions(self, symbol: Optional[str] = None, magic: Optional[int] = None):
        with self._active_scope() as (
            alias,
            trading_service,
            _account_service,
        ):
            payload = {"account_alias": alias, "symbol": symbol, "magic": magic}
            return self._execute("get_positions", alias, payload, lambda: trading_service.get_positions(symbol, magic))

    def get_orders(self, symbol: Optional[str] = None, magic: Optional[int] = None):
        with self._active_scope() as (
            alias,
            trading_service,
            _account_service,
        ):
            payload = {"account_alias": alias, "symbol": symbol, "magic": magic}
            return self._execute("get_orders", alias, payload, lambda: trading_service.get_orders(symbol, magic))

    def get_position_close_details(
        self,
        ticket: int,
        *,
        symbol: Optional[str] = None,
        lookback_days: int = 7,
    ) -> Optional[dict[str, Any]]:
        with self._active_scope() as (
            alias,
            trading_service,
            _account_service,
        ):
            payload = {
                "account_alias": alias,
                "ticket": ticket,
                "symbol": symbol,
                "lookback_days": lookback_days,
            }
            return self._execute(
                "get_position_close_details",
                alias,
                payload,
                lambda: trading_service.get_position_close_details(
                    ticket=ticket,
                    symbol=symbol,
                    lookback_days=lookback_days,
                ),
            )

    def resolve_position_context(
        self,
        *,
        ticket: int,
        comment: Optional[str] = None,
        limit: int = 500,
    ) -> Optional[dict[str, Any]]:
        for row in self.recent_operations(
            operation_type="execute_trade",
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
            signal_meta = metadata.get("signal") or {}
            request_id = str(request_payload.get("request_id") or "").strip()
            if not request_id:
                request_id = str(signal_meta.get("signal_id") or f"restored:{ticket}")
            return {
                "signal_id": request_id,
                "timeframe": str(signal_meta.get("timeframe") or ""),
                "strategy": str(signal_meta.get("strategy") or ""),
                "confidence": signal_meta.get("confidence"),
                "regime": metadata.get("regime"),
                "fill_price": response_payload.get("fill_price") or response_payload.get("price"),
                "comment": payload_comment or comment,
                "source": "restored_signal_trade" if signal_meta else "restored_trade",
                "entry_origin": self._entry_origin(request_payload),
                "request_id": request_id,
            }
        return None

    def recent_operations(
        self,
        *,
        operation_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        if self.db_writer is None:
            return []
        rows = self.db_writer.fetch_trade_operations(
            account_alias=self.active_account_alias,
            operation_type=operation_type,
            status=status,
            limit=limit,
        )
        return [
            {
                "recorded_at": row[0].isoformat() if row[0] else None,
                "operation_id": row[1],
                "account_alias": row[2],
                "operation_type": row[3],
                "status": row[4],
                "symbol": row[5],
                "side": row[6],
                "order_kind": row[7],
                "volume": row[8],
                "ticket": row[9],
                "order_id": row[10],
                "deal_id": row[11],
                "magic": row[12],
                "duration_ms": row[13],
                "error_message": row[14],
                "request_payload": row[15] or {},
                "response_payload": row[16] or {},
            }
            for row in rows
        ]

    def monitoring_summary(self, *, hours: int = 24) -> dict:
        if self.db_writer is None:
            return {
                "active_account_alias": self.active_account_alias,
                "accounts": self.list_accounts(),
                "trade_control": self.trade_control_status(),
                "summary": [],
                "recent": [],
            }
        rows = self.db_writer.summarize_trade_operations(
            hours=hours,
            account_alias=self.active_account_alias,
        )
        return {
            "active_account_alias": self.active_account_alias,
            "accounts": self.list_accounts(),
            "trade_control": self.trade_control_status(),
            "daily": self.daily_trade_summary(),
            "summary": [
                {
                    "account_alias": row[0],
                    "operation_type": row[1],
                    "status": row[2],
                    "count": int(row[3] or 0),
                    "avg_duration_ms": float(row[4] or 0.0),
                    "last_seen_at": row[5].isoformat() if row[5] else None,
                }
                for row in rows
            ],
            "recent": self.recent_operations(limit=20),
        }
