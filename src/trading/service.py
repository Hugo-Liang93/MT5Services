from __future__ import annotations

import logging
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from src.persistence.db import TimescaleWriter

from .models import TradeOperationRecord
from .registry import TradingAccountRegistry

logger = logging.getLogger(__name__)


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

    def _execute(self, operation_type: str, account_alias: Optional[str], payload: Dict[str, Any], fn):
        started = time.monotonic()
        resolved_alias = self.registry.resolve_alias(account_alias)
        try:
            result = fn()
            duration_ms = int((time.monotonic() - started) * 1000)
            self._record_operation(
                TradeOperationRecord(
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
            )
            return result
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            self._record_operation(
                TradeOperationRecord(
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
                    request_payload=payload,
                    response_payload={},
                )
            )
            raise

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
