from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence


class TradingFlowTraceReadModel:
    """交易主链路 trace 只读投影。

    目标不是新增事实源，而是把现有信号、执行、交易审计、挂单状态、
    持仓状态和结果记录聚合成一条可审查、可视化的时间线。
    """

    def __init__(
        self,
        *,
        signal_repo: Any,
        command_audit_repo: Any,
        trading_state_repo: Any,
        account_alias_getter: Callable[[], str],
    ) -> None:
        self._signal_repo = signal_repo
        self._command_audit_repo = command_audit_repo
        self._trading_state_repo = trading_state_repo
        self._account_alias_getter = account_alias_getter

    def trace_by_signal_id(self, signal_id: str) -> dict[str, Any]:
        normalized_signal_id = str(signal_id or "").strip()
        preview_signal = self._signal_repo.fetch_signal_event_by_id(
            signal_id=normalized_signal_id,
            scope="preview",
        )
        confirmed_signal = self._signal_repo.fetch_signal_event_by_id(
            signal_id=normalized_signal_id,
            scope="confirmed",
        )
        auto_executions = self._signal_repo.fetch_auto_executions(
            signal_id=normalized_signal_id,
            limit=50,
        )
        signal_outcomes = self._signal_repo.fetch_signal_outcomes(
            signal_id=normalized_signal_id,
            limit=20,
        )
        trade_outcomes = self._signal_repo.fetch_trade_outcomes(
            signal_id=normalized_signal_id,
            limit=20,
        )
        pending_orders = self._trading_state_repo.fetch_pending_order_states(
            account_alias=self._account_alias_getter(),
            signal_id=normalized_signal_id,
            limit=100,
        )
        positions = self._trading_state_repo.fetch_position_runtime_states(
            account_alias=self._account_alias_getter(),
            signal_id=normalized_signal_id,
            limit=100,
        )
        operations = self._command_audit_repo.fetch_trace_operations(
            account_alias=self._account_alias_getter(),
            signal_id=normalized_signal_id,
            limit=100,
        )

        facts = {
            "signal_preview": preview_signal,
            "signal_confirmed": confirmed_signal,
            "auto_executions": auto_executions,
            "trade_command_audits": operations,
            "pending_orders": pending_orders,
            "positions": positions,
            "signal_outcomes": signal_outcomes,
            "trade_outcomes": trade_outcomes,
        }
        identifiers = self._build_identifiers(
            signal_id=normalized_signal_id,
            operations=operations,
            pending_orders=pending_orders,
            positions=positions,
        )
        timeline = self._build_timeline(
            signal_id=normalized_signal_id,
            preview_signal=preview_signal,
            confirmed_signal=confirmed_signal,
            auto_executions=auto_executions,
            operations=operations,
            pending_orders=pending_orders,
            positions=positions,
            signal_outcomes=signal_outcomes,
            trade_outcomes=trade_outcomes,
        )
        return {
            "signal_id": normalized_signal_id,
            "found": any(
                [
                    preview_signal,
                    confirmed_signal,
                    auto_executions,
                    operations,
                    pending_orders,
                    positions,
                    signal_outcomes,
                    trade_outcomes,
                ]
            ),
            "identifiers": identifiers,
            "summary": self._build_summary(facts=facts),
            "timeline": timeline,
            "graph": self._build_graph(timeline),
            "facts": facts,
        }

    def _build_summary(self, *, facts: dict[str, Any]) -> dict[str, Any]:
        operations = list(facts.get("trade_command_audits") or [])
        pending_orders = list(facts.get("pending_orders") or [])
        positions = list(facts.get("positions") or [])
        return {
            "stages": {
                "preview_signal": "present" if facts.get("signal_preview") else "missing",
                "confirmed_signal": (
                    "present" if facts.get("signal_confirmed") else "missing"
                ),
                "auto_execution": (
                    "present" if facts.get("auto_executions") else "missing"
                ),
                "trade_command_audit": "present" if operations else "missing",
                "pending_order": "present" if pending_orders else "missing",
                "position_runtime": "present" if positions else "missing",
                "signal_outcome": (
                    "present" if facts.get("signal_outcomes") else "missing"
                ),
                "trade_outcome": (
                    "present" if facts.get("trade_outcomes") else "missing"
                ),
            },
            "command_counts": self._count_by_key(operations, "command_type"),
            "pending_status_counts": self._count_by_key(pending_orders, "status"),
            "position_status_counts": self._count_by_key(positions, "status"),
        }

    def _build_identifiers(
        self,
        *,
        signal_id: str,
        operations: Sequence[dict[str, Any]],
        pending_orders: Sequence[dict[str, Any]],
        positions: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        request_ids = {signal_id}
        operation_ids: set[str] = set()
        order_tickets: set[int] = set()
        position_tickets: set[int] = set()

        for row in operations:
            operation_id = str(row.get("operation_id") or "").strip()
            if operation_id:
                operation_ids.add(operation_id)
            request_payload = row.get("request_payload") or {}
            response_payload = row.get("response_payload") or {}
            for candidate in (
                request_payload.get("request_id"),
                response_payload.get("request_id"),
                response_payload.get("trace_id"),
            ):
                value = str(candidate or "").strip()
                if value:
                    request_ids.add(value)
            for key in ("ticket", "order_id"):
                ticket = self._int_or_none(row.get(key))
                if ticket:
                    order_tickets.add(ticket)
            deal_ticket = self._int_or_none(row.get("ticket"))
            if deal_ticket:
                order_tickets.add(deal_ticket)

        for row in pending_orders:
            ticket = self._int_or_none(row.get("order_ticket"))
            if ticket:
                order_tickets.add(ticket)
            position_ticket = self._int_or_none(row.get("position_ticket"))
            if position_ticket:
                position_tickets.add(position_ticket)

        for row in positions:
            ticket = self._int_or_none(row.get("position_ticket"))
            if ticket:
                position_tickets.add(ticket)
            order_ticket = self._int_or_none(row.get("order_ticket"))
            if order_ticket:
                order_tickets.add(order_ticket)

        return {
            "signal_id": signal_id,
            "request_ids": sorted(request_ids),
            "operation_ids": sorted(operation_ids),
            "order_tickets": sorted(order_tickets),
            "position_tickets": sorted(position_tickets),
        }

    def _build_timeline(
        self,
        *,
        signal_id: str,
        preview_signal: Optional[dict[str, Any]],
        confirmed_signal: Optional[dict[str, Any]],
        auto_executions: Sequence[dict[str, Any]],
        operations: Sequence[dict[str, Any]],
        pending_orders: Sequence[dict[str, Any]],
        positions: Sequence[dict[str, Any]],
        signal_outcomes: Sequence[dict[str, Any]],
        trade_outcomes: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if preview_signal is not None:
            events.append(
                self._timeline_event(
                    event_id=f"{signal_id}:signal_preview",
                    stage="signal.preview",
                    status="preview",
                    at=preview_signal.get("generated_at"),
                    source="signal_preview_events",
                    summary="预览信号生成",
                    details=preview_signal,
                )
            )
        if confirmed_signal is not None:
            events.append(
                self._timeline_event(
                    event_id=f"{signal_id}:signal_confirmed",
                    stage="signal.confirmed",
                    status="confirmed",
                    at=confirmed_signal.get("generated_at"),
                    source="signal_events",
                    summary="确认信号生成",
                    details=confirmed_signal,
                )
            )
        for row in auto_executions:
            executed_at = row.get("executed_at")
            success = bool(row.get("success"))
            events.append(
                self._timeline_event(
                    event_id=f"{signal_id}:auto_execution:{self._datetime_key(executed_at)}",
                    stage="execution.auto",
                    status="success" if success else "failed",
                    at=executed_at,
                    source="auto_executions",
                    summary="自动执行尝试",
                    details=row,
                )
            )
        for row in operations:
            operation_type = str(row.get("command_type") or "unknown")
            events.append(
                self._timeline_event(
                    event_id=f"{signal_id}:operation:{row.get('operation_id')}",
                    stage=f"trade.{operation_type}",
                    status=str(row.get("status") or "unknown"),
                    at=row.get("recorded_at"),
                    source="trade_command_audits",
                    summary=f"交易命令: {operation_type}",
                    details=row,
                )
            )
        for row in pending_orders:
            status = str(row.get("status") or "unknown")
            events.append(
                self._timeline_event(
                    event_id=f"{signal_id}:pending:{row.get('order_ticket')}:{status}",
                    stage=f"pending.{status}",
                    status=status,
                    at=self._pending_event_time(row),
                    source="pending_order_states",
                    summary=f"挂单状态: {status}",
                    details=row,
                )
            )
        for row in positions:
            status = str(row.get("status") or "unknown")
            events.append(
                self._timeline_event(
                    event_id=f"{signal_id}:position:{row.get('position_ticket')}:{status}",
                    stage=f"position.{status}",
                    status=status,
                    at=row.get("closed_at") or row.get("opened_at") or row.get("updated_at"),
                    source="position_runtime_states",
                    summary=f"持仓状态: {status}",
                    details=row,
                )
            )
        for row in signal_outcomes:
            events.append(
                self._timeline_event(
                    event_id=f"{signal_id}:signal_outcome:{self._datetime_key(row.get('recorded_at'))}",
                    stage="outcome.signal",
                    status="won" if row.get("won") is True else "lost" if row.get("won") is False else "unknown",
                    at=row.get("recorded_at"),
                    source="signal_outcomes",
                    summary="信号事后评估",
                    details=row,
                )
            )
        for row in trade_outcomes:
            events.append(
                self._timeline_event(
                    event_id=f"{signal_id}:trade_outcome:{self._datetime_key(row.get('recorded_at'))}",
                    stage="outcome.trade",
                    status="won" if row.get("won") is True else "lost" if row.get("won") is False else "unknown",
                    at=row.get("recorded_at"),
                    source="trade_outcomes",
                    summary="真实交易结果",
                    details=row,
                )
            )
        events.sort(
            key=lambda item: (
                1 if item["at"] is None else 0,
                item["at"] or "",
                item["id"],
            )
        )
        return events

    def _build_graph(self, timeline: Sequence[dict[str, Any]]) -> dict[str, Any]:
        nodes = [
            {
                "id": item["id"],
                "label": item["summary"],
                "stage": item["stage"],
                "status": item["status"],
                "at": item["at"],
                "source": item["source"],
            }
            for item in timeline
        ]
        edges = []
        for index in range(1, len(timeline)):
            previous = timeline[index - 1]
            current = timeline[index]
            edges.append(
                {
                    "from": previous["id"],
                    "to": current["id"],
                    "relation": "next",
                }
            )
        return {"nodes": nodes, "edges": edges}

    @staticmethod
    def _count_by_key(rows: Sequence[dict[str, Any]], key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            normalized = str(row.get(key) or "unknown")
            counts[normalized] = counts.get(normalized, 0) + 1
        return counts

    def _timeline_event(
        self,
        *,
        event_id: str,
        stage: str,
        status: str,
        at: Any,
        source: str,
        summary: str,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "id": event_id,
            "stage": stage,
            "status": status,
            "at": self._normalize_datetime(at),
            "source": source,
            "summary": summary,
            "details": self._json_safe(details),
        }

    def _pending_event_time(self, row: dict[str, Any]) -> Any:
        status = str(row.get("status") or "")
        if status == "filled":
            return row.get("filled_at") or row.get("updated_at")
        if status in {"expired", "cancelled"}:
            return row.get("cancelled_at") or row.get("updated_at")
        return row.get("created_at") or row.get("updated_at")

    @staticmethod
    def _normalize_datetime(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat()
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return str(value)
        return parsed.astimezone(timezone.utc).isoformat()

    @classmethod
    def _json_safe(cls, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, datetime):
            return cls._normalize_datetime(value)
        if isinstance(value, dict):
            return {str(k): cls._json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [cls._json_safe(item) for item in value]
        return str(value)

    @staticmethod
    def _datetime_key(value: Any) -> str:
        normalized = TradingFlowTraceReadModel._normalize_datetime(value)
        return str(normalized or "na").replace(":", "_")

    @staticmethod
    def _int_or_none(value: Any) -> Optional[int]:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            return None
        return normalized if normalized > 0 else None
