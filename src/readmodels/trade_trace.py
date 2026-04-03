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
        pipeline_trace_repo: Any,
        trading_state_repo: Any,
        account_alias_getter: Callable[[], str],
    ) -> None:
        self._signal_repo = signal_repo
        self._command_audit_repo = command_audit_repo
        self._pipeline_trace_repo = pipeline_trace_repo
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
        trace_ids = self._collect_trace_ids(
            preview_signals=[preview_signal] if preview_signal is not None else [],
            confirmed_signals=[confirmed_signal] if confirmed_signal is not None else [],
            operations=operations,
        )
        pipeline_events = self._pipeline_trace_repo.fetch_pipeline_trace_events(
            trace_ids=trace_ids,
            limit=500,
        )

        return self._build_trace_response(
            signal_id=normalized_signal_id,
            trace_id=trace_ids[0] if len(trace_ids) == 1 else None,
            preview_signals=[preview_signal] if preview_signal is not None else [],
            confirmed_signals=[confirmed_signal] if confirmed_signal is not None else [],
            pipeline_events=pipeline_events,
            auto_executions=auto_executions,
            operations=operations,
            pending_orders=pending_orders,
            positions=positions,
            signal_outcomes=signal_outcomes,
            trade_outcomes=trade_outcomes,
        )

    def trace_by_trace_id(self, trace_id: str) -> dict[str, Any]:
        normalized_trace_id = str(trace_id or "").strip()
        preview_signals = self._signal_repo.fetch_signal_events_by_trace_id(
            trace_id=normalized_trace_id,
            scope="preview",
            limit=50,
        )
        confirmed_signals = self._signal_repo.fetch_signal_events_by_trace_id(
            trace_id=normalized_trace_id,
            scope="confirmed",
            limit=50,
        )
        operations = self._command_audit_repo.fetch_trace_operations_by_trace_id(
            account_alias=self._account_alias_getter(),
            trace_id=normalized_trace_id,
            limit=100,
        )
        signal_ids = self._collect_signal_ids(
            preview_signals=preview_signals,
            confirmed_signals=confirmed_signals,
            operations=operations,
        )
        auto_executions = self._fetch_by_signal_ids(
            signal_ids=signal_ids,
            fetcher=self._signal_repo.fetch_auto_executions,
            limit=50,
        )
        signal_outcomes = self._fetch_by_signal_ids(
            signal_ids=signal_ids,
            fetcher=self._signal_repo.fetch_signal_outcomes,
            limit=20,
        )
        trade_outcomes = self._fetch_by_signal_ids(
            signal_ids=signal_ids,
            fetcher=self._signal_repo.fetch_trade_outcomes,
            limit=20,
        )
        pending_orders = self._fetch_state_by_signal_ids(
            signal_ids=signal_ids,
            fetcher=self._trading_state_repo.fetch_pending_order_states,
            limit=100,
        )
        positions = self._fetch_state_by_signal_ids(
            signal_ids=signal_ids,
            fetcher=self._trading_state_repo.fetch_position_runtime_states,
            limit=100,
        )
        pipeline_events = self._pipeline_trace_repo.fetch_pipeline_trace_events(
            trace_ids=[normalized_trace_id],
            limit=500,
        )
        primary_signal_id = signal_ids[0] if signal_ids else None
        return self._build_trace_response(
            signal_id=primary_signal_id,
            trace_id=normalized_trace_id,
            preview_signals=preview_signals,
            confirmed_signals=confirmed_signals,
            pipeline_events=pipeline_events,
            auto_executions=auto_executions,
            operations=operations,
            pending_orders=pending_orders,
            positions=positions,
            signal_outcomes=signal_outcomes,
            trade_outcomes=trade_outcomes,
        )

    def _build_trace_response(
        self,
        *,
        signal_id: str | None,
        trace_id: str | None,
        preview_signals: Sequence[dict[str, Any]],
        confirmed_signals: Sequence[dict[str, Any]],
        pipeline_events: Sequence[dict[str, Any]],
        auto_executions: Sequence[dict[str, Any]],
        operations: Sequence[dict[str, Any]],
        pending_orders: Sequence[dict[str, Any]],
        positions: Sequence[dict[str, Any]],
        signal_outcomes: Sequence[dict[str, Any]],
        trade_outcomes: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        normalized_signal_id = str(signal_id or "").strip() or None
        normalized_trace_id = str(trace_id or "").strip() or None
        preview_signal = preview_signals[0] if preview_signals else None
        confirmed_signal = confirmed_signals[0] if confirmed_signals else None
        trace_ids = self._collect_trace_ids(
            preview_signals=preview_signals,
            confirmed_signals=confirmed_signals,
            operations=operations,
        )
        signal_ids = self._collect_signal_ids(
            preview_signals=preview_signals,
            confirmed_signals=confirmed_signals,
            operations=operations,
        )
        facts = {
            "signal_preview": preview_signal,
            "signal_confirmed": confirmed_signal,
            "signal_preview_events": list(preview_signals),
            "signal_confirmed_events": list(confirmed_signals),
            "pipeline_trace_events": list(pipeline_events),
            "auto_executions": list(auto_executions),
            "trade_command_audits": list(operations),
            "pending_orders": list(pending_orders),
            "positions": list(positions),
            "signal_outcomes": list(signal_outcomes),
            "trade_outcomes": list(trade_outcomes),
        }
        identifiers = self._build_identifiers(
            signal_id=normalized_signal_id,
            signal_ids=signal_ids,
            trace_ids=trace_ids,
            operations=operations,
            pending_orders=pending_orders,
            positions=positions,
        )
        timeline = self._build_timeline(
            signal_id=normalized_signal_id or normalized_trace_id or "trace",
            preview_signals=preview_signals,
            confirmed_signals=confirmed_signals,
            pipeline_events=pipeline_events,
            auto_executions=auto_executions,
            operations=operations,
            pending_orders=pending_orders,
            positions=positions,
            signal_outcomes=signal_outcomes,
            trade_outcomes=trade_outcomes,
        )
        return {
            "signal_id": normalized_signal_id,
            "trace_id": normalized_trace_id,
            "found": any(
                [
                    preview_signals,
                    confirmed_signals,
                    pipeline_events,
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
        pipeline_events = list(facts.get("pipeline_trace_events") or [])
        pending_orders = list(facts.get("pending_orders") or [])
        positions = list(facts.get("positions") or [])
        return {
            "stages": {
                "pipeline_bar_closed": (
                    "present"
                    if self._has_pipeline_stage(pipeline_events, "bar_closed")
                    else "missing"
                ),
                "pipeline_indicator_computed": (
                    "present"
                    if self._has_pipeline_stage(pipeline_events, "indicator_computed")
                    else "missing"
                ),
                "pipeline_snapshot_published": (
                    "present"
                    if self._has_pipeline_stage(pipeline_events, "snapshot_published")
                    else "missing"
                ),
                "pipeline_signal_filter": (
                    "present"
                    if self._has_pipeline_stage(pipeline_events, "signal_filter_decided")
                    else "missing"
                ),
                "pipeline_signal_evaluated": (
                    "present"
                    if self._has_pipeline_stage(pipeline_events, "signal_evaluated")
                    else "missing"
                ),
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
            "pipeline_event_counts": self._count_by_key(pipeline_events, "event_type"),
            "command_counts": self._count_by_key(operations, "command_type"),
            "pending_status_counts": self._count_by_key(pending_orders, "status"),
            "position_status_counts": self._count_by_key(positions, "status"),
        }

    def _build_identifiers(
        self,
        *,
        signal_id: str | None,
        signal_ids: Sequence[str],
        operations: Sequence[dict[str, Any]],
        trace_ids: Sequence[str],
        pending_orders: Sequence[dict[str, Any]],
        positions: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        request_ids = {
            str(item).strip()
            for item in [signal_id, *signal_ids]
            if str(item or "").strip()
        }
        normalized_trace_ids = {
            str(item).strip() for item in trace_ids if str(item or "").strip()
        }
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
            "signal_ids": sorted({str(item).strip() for item in signal_ids if str(item or "").strip()}),
            "request_ids": sorted(request_ids),
            "trace_ids": sorted(normalized_trace_ids),
            "operation_ids": sorted(operation_ids),
            "order_tickets": sorted(order_tickets),
            "position_tickets": sorted(position_tickets),
        }

    def _build_timeline(
        self,
        *,
        signal_id: str,
        preview_signals: Sequence[dict[str, Any]],
        confirmed_signals: Sequence[dict[str, Any]],
        pipeline_events: Sequence[dict[str, Any]],
        auto_executions: Sequence[dict[str, Any]],
        operations: Sequence[dict[str, Any]],
        pending_orders: Sequence[dict[str, Any]],
        positions: Sequence[dict[str, Any]],
        signal_outcomes: Sequence[dict[str, Any]],
        trade_outcomes: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for row in pipeline_events:
            event_type = str(row.get("event_type") or "unknown")
            stage = self._pipeline_stage_name(event_type)
            events.append(
                self._timeline_event(
                    event_id=(
                        f"{signal_id}:pipeline:{row.get('trace_id')}:{row.get('id') or self._datetime_key(row.get('recorded_at'))}:{event_type}"
                    ),
                    stage=stage,
                    status=self._pipeline_status(row),
                    at=row.get("recorded_at"),
                    source="pipeline_trace_events",
                    summary=self._pipeline_summary(row),
                    details=row,
                )
            )
        for index, preview_signal in enumerate(preview_signals):
            events.append(
                self._timeline_event(
                    event_id=f"{signal_id}:signal_preview:{index}",
                    stage="signal.preview",
                    status="preview",
                    at=preview_signal.get("generated_at"),
                    source="signal_preview_events",
                    summary="预览信号生成",
                    details=preview_signal,
                )
            )
        for index, confirmed_signal in enumerate(confirmed_signals):
            events.append(
                self._timeline_event(
                    event_id=f"{signal_id}:signal_confirmed:{index}",
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

    def _collect_trace_ids(
        self,
        *,
        preview_signals: Sequence[dict[str, Any]],
        confirmed_signals: Sequence[dict[str, Any]],
        operations: Sequence[dict[str, Any]],
    ) -> list[str]:
        trace_ids: set[str] = set()
        for row in [*preview_signals, *confirmed_signals]:
            if not row:
                continue
            metadata = row.get("metadata") or {}
            trace_id = str(metadata.get("signal_trace_id") or "").strip()
            if trace_id:
                trace_ids.add(trace_id)
        for row in operations:
            for payload in (row.get("request_payload") or {}, row.get("response_payload") or {}):
                trace_id = str(payload.get("trace_id") or "").strip()
                if trace_id:
                    trace_ids.add(trace_id)
        return sorted(trace_ids)

    def _collect_signal_ids(
        self,
        *,
        preview_signals: Sequence[dict[str, Any]],
        confirmed_signals: Sequence[dict[str, Any]],
        operations: Sequence[dict[str, Any]],
    ) -> list[str]:
        signal_ids: set[str] = set()
        for row in [*preview_signals, *confirmed_signals]:
            signal_id = str(row.get("signal_id") or "").strip()
            if signal_id:
                signal_ids.add(signal_id)
        for row in operations:
            for payload in (row.get("request_payload") or {}, row.get("response_payload") or {}):
                metadata = payload.get("metadata") or {}
                signal = metadata.get("signal") or {}
                signal_id = str(signal.get("signal_id") or "").strip()
                if signal_id:
                    signal_ids.add(signal_id)
                direct = str(payload.get("request_id") or "").strip()
                if direct:
                    signal_ids.add(direct)
        return sorted(signal_ids)

    @staticmethod
    def _fetch_by_signal_ids(
        *,
        signal_ids: Sequence[str],
        fetcher: Any,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for signal_id in signal_ids:
            for row in fetcher(signal_id=signal_id, limit=limit):
                key = (
                    str(row.get("signal_id") or ""),
                    str(row.get("recorded_at") or row.get("executed_at") or ""),
                )
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
        rows.sort(
            key=lambda item: str(
                item.get("recorded_at")
                or item.get("executed_at")
                or item.get("generated_at")
                or ""
            )
        )
        return rows

    def _fetch_state_by_signal_ids(
        self,
        *,
        signal_ids: Sequence[str],
        fetcher: Any,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for signal_id in signal_ids:
            for row in fetcher(
                account_alias=self._account_alias_getter(),
                signal_id=signal_id,
                limit=limit,
            ):
                ticket = str(
                    row.get("order_ticket")
                    or row.get("position_ticket")
                    or row.get("signal_id")
                    or ""
                )
                if ticket in seen:
                    continue
                seen.add(ticket)
                rows.append(row)
        return rows

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

    @staticmethod
    def _has_pipeline_stage(rows: Sequence[dict[str, Any]], event_type: str) -> bool:
        target = str(event_type).strip()
        return any(str(row.get("event_type") or "").strip() == target for row in rows)

    @staticmethod
    def _pipeline_stage_name(event_type: str) -> str:
        normalized = str(event_type or "unknown").strip()
        mapping = {
            "bar_closed": "pipeline.bar_closed",
            "indicator_computed": "pipeline.indicator_computed",
            "snapshot_published": "pipeline.snapshot_published",
            "signal_filter_decided": "pipeline.signal_filter",
            "signal_evaluated": "pipeline.signal_evaluated",
        }
        return mapping.get(normalized, f"pipeline.{normalized}")

    @staticmethod
    def _pipeline_status(row: dict[str, Any]) -> str:
        event_type = str(row.get("event_type") or "")
        payload = row.get("payload") or {}
        if event_type == "signal_filter_decided":
            return "passed" if payload.get("allowed") else "blocked"
        if event_type == "signal_evaluated":
            return str(payload.get("signal_state") or "evaluated")
        return "observed"

    @classmethod
    def _pipeline_summary(cls, row: dict[str, Any]) -> str:
        event_type = str(row.get("event_type") or "")
        payload = row.get("payload") or {}
        if event_type == "bar_closed":
            return "行情 bar close"
        if event_type == "indicator_computed":
            return "指标计算完成"
        if event_type == "snapshot_published":
            return "指标快照发布"
        if event_type == "signal_filter_decided":
            return (
                f"信号过滤通过: {payload.get('category') or 'pass'}"
                if payload.get("allowed")
                else f"信号过滤拦截: {payload.get('reason') or 'unknown'}"
            )
        if event_type == "signal_evaluated":
            strategy = payload.get("strategy") or "unknown"
            direction = payload.get("direction") or "hold"
            return f"策略评估: {strategy}/{direction}"
        return f"Pipeline 事件: {event_type}"

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
