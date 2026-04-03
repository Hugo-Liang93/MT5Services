from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from threading import RLock
from typing import Any, Callable, Optional

from src.persistence.db import TimescaleWriter

from .models import TradeCommandAuditRecord


class TradeCommandAuditService:
    """交易命令审计服务。"""

    def __init__(
        self,
        db_writer: Optional[TimescaleWriter],
        *,
        account_alias_getter: Callable[[], str],
    ) -> None:
        self._db_writer = db_writer
        self._account_alias_getter = account_alias_getter

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

    def record(self, record: TradeCommandAuditRecord) -> None:
        if self._db_writer is None:
            return
        normalized = TradeCommandAuditRecord(
            account_alias=record.account_alias,
            command_type=record.command_type,
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
        self._db_writer.write_trade_command_audits([normalized.to_row()])

    def fetch_successful_trade_result(
        self,
        *,
        request_id: str,
        limit: int,
    ) -> Optional[dict[str, Any]]:
        if self._db_writer is None:
            return None
        rows = self._db_writer.fetch_trade_command_audits(
            account_alias=self._account_alias_getter(),
            command_type="execute_trade",
            status="success",
            limit=limit,
        )
        normalized_request_id = str(request_id or "").strip()
        for row in rows:
            request_payload = row[15] or {}
            response_payload = row[16] or {}
            if str(request_payload.get("request_id") or "").strip() != normalized_request_id:
                continue
            if not isinstance(response_payload, dict):
                continue
            replayed = dict(response_payload)
            replayed.setdefault("operation_id", row[1])
            replayed["idempotent_replay"] = True
            replayed["idempotent_source"] = "audit"
            return replayed
        return None

    def recent_command_audits(
        self,
        *,
        command_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        if self._db_writer is None:
            return []
        rows = self._db_writer.fetch_trade_command_audits(
            account_alias=self._account_alias_getter(),
            command_type=command_type,
            status=status,
            limit=limit,
        )
        return [
            {
                "recorded_at": row[0].isoformat() if row[0] else None,
                "operation_id": row[1],
                "account_alias": row[2],
                "command_type": row[3],
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

    def summarize_operations(self, *, hours: int = 24) -> list[dict[str, Any]]:
        if self._db_writer is None:
            return []
        rows = self._db_writer.summarize_trade_command_audits(
            hours=hours,
            account_alias=self._account_alias_getter(),
        )
        return [
            {
                "account_alias": row[0],
                "command_type": row[1],
                "status": row[2],
                "count": int(row[3] or 0),
                "avg_duration_ms": float(row[4] or 0.0),
                "last_seen_at": row[5].isoformat() if row[5] else None,
            }
            for row in rows
        ]


class TradeDailyStatsService:
    """交易日内统计服务。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._daily_stats: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "total": 0,
                "success": 0,
                "failed": 0,
                "symbols": {},
                "operations": {},
                "risk": {"blocked": 0, "warn": 0, "allow": 0},
                "last_trade_at": None,
            }
        )

    def update(self, record: TradeCommandAuditRecord) -> None:
        if record.command_type not in {
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
        with self._lock:
            bucket = self._daily_stats[day]
            bucket["total"] += 1
            if record.status == "success":
                bucket["success"] += 1
            else:
                bucket["failed"] += 1
            symbol = record.symbol or "unknown"
            symbol_stats = bucket["symbols"].setdefault(
                symbol,
                {"total": 0, "success": 0, "failed": 0},
            )
            symbol_stats["total"] += 1
            if record.status == "success":
                symbol_stats["success"] += 1
            else:
                symbol_stats["failed"] += 1
            op_stats = bucket["operations"].setdefault(
                record.command_type,
                {"total": 0, "success": 0, "failed": 0},
            )
            op_stats["total"] += 1
            if record.status == "success":
                op_stats["success"] += 1
            else:
                op_stats["failed"] += 1
            if record.command_type == "precheck_trade" and isinstance(record.response_payload, dict):
                action = str(record.response_payload.get("verdict") or "allow").lower()
                if action not in {"allow", "warn", "block"}:
                    action = "allow"
                bucket["risk"]["blocked" if action == "block" else action] += 1
            bucket["last_trade_at"] = (record.recorded_at or datetime.now(timezone.utc)).isoformat()

    def summary(
        self,
        *,
        account_alias: str,
        summary_date: Optional[date] = None,
    ) -> dict[str, Any]:
        day_key = (summary_date or datetime.now(timezone.utc).date()).isoformat()
        with self._lock:
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
            "account_alias": account_alias,
            **snapshot,
            "success_rate": round((success / total) * 100, 2) if total else 0.0,
        }
