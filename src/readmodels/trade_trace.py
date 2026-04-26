from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

from src.monitoring.pipeline import (
    PIPELINE_STAGE_DEFINITIONS,
    pipeline_event_status,
    pipeline_event_summary,
    pipeline_stage_name,
    pipeline_stage_presence,
)


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
        # §0v P2：auto_executions / trade_outcomes 必须按 account_alias 过滤，
        # 否则多账户共享 signal_id 时把别人的执行/结果混进当前账户 facts/timeline。
        # signal_outcomes 是 signal-level 不带 account 字段，无需过滤。
        current_account_alias = self._account_alias_getter()
        auto_executions = self._signal_repo.fetch_auto_executions(
            signal_id=normalized_signal_id,
            limit=50,
            account_alias=current_account_alias,
        )
        signal_outcomes = self._signal_repo.fetch_signal_outcomes(
            signal_id=normalized_signal_id,
            limit=20,
        )
        trade_outcomes = self._signal_repo.fetch_trade_outcomes(
            signal_id=normalized_signal_id,
            limit=20,
            account_alias=current_account_alias,
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
        # §0v P2：同 trace_by_signal_id 修复——按 account_alias 过滤
        current_account_alias = self._account_alias_getter()
        auto_executions = self._fetch_by_signal_ids(
            signal_ids=signal_ids,
            fetcher=self._signal_repo.fetch_auto_executions,
            limit=50,
            account_alias=current_account_alias,
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
            account_alias=current_account_alias,
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

    def list_traces(
        self,
        *,
        trace_id: str | None = None,
        signal_id: str | None = None,
        symbol: str | None = None,
        timeframe: str | None = None,
        strategy: str | None = None,
        status: str | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        page: int = 1,
        page_size: int = 100,
        sort: str = "last_event_at_desc",
    ) -> dict[str, Any]:
        query_result = self._pipeline_trace_repo.query_trace_summaries(
            trace_id=trace_id,
            signal_id=signal_id,
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            status=status,
            from_time=from_time,
            to_time=to_time,
            page=page,
            page_size=page_size,
            sort=sort,
        )
        items = [self._build_trace_summary_item(row) for row in query_result.get("items", [])]
        return {
            "items": items,
            "total": int(query_result.get("total") or 0),
            "page": int(query_result.get("page") or page),
            "page_size": int(query_result.get("page_size") or page_size),
        }

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
        admission_reports = self._extract_admission_reports(pipeline_events)
        facts = {
            "signal_preview": preview_signal,
            "signal_confirmed": confirmed_signal,
            "signal_preview_events": list(preview_signals),
            "signal_confirmed_events": list(confirmed_signals),
            "admission_reports": admission_reports,
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
        trace_status = self._derive_trace_status(
            timeline=timeline,
            pipeline_events=pipeline_events,
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
            "summary": self._build_summary(
                facts=facts,
                timeline=timeline,
                status=trace_status["status"],
                started_at=trace_status["started_at"],
                last_event_at=trace_status["last_event_at"],
                event_count=trace_status["event_count"],
                last_stage=trace_status["last_stage"],
                reason=trace_status["reason"],
            ),
            "timeline": timeline,
            "graph": self._build_graph(timeline),
            "facts": facts,
            "related_signals": {
                "preview": list(preview_signals),
                "confirmed": list(confirmed_signals),
                "signal_outcomes": list(signal_outcomes),
                "trade_outcomes": list(trade_outcomes),
            },
            "related_trade_audits": list(operations),
            "related_pipeline_events": list(pipeline_events),
        }

    def _build_summary(
        self,
        *,
        facts: dict[str, Any],
        timeline: Sequence[dict[str, Any]],
        status: str,
        started_at: str | None,
        last_event_at: str | None,
        event_count: int,
        last_stage: str | None,
        reason: str | None,
    ) -> dict[str, Any]:
        operations = list(facts.get("trade_command_audits") or [])
        pipeline_events = list(facts.get("pipeline_trace_events") or [])
        pending_orders = list(facts.get("pending_orders") or [])
        positions = list(facts.get("positions") or [])
        admission_reports = list(facts.get("admission_reports") or [])
        latest_admission = admission_reports[-1] if admission_reports else None
        return {
            "stages": {
                **{
                    definition.summary_key: (
                        "present"
                        if pipeline_stage_presence(
                            pipeline_events,
                            definition.summary_key,
                        )
                        else "missing"
                    )
                    for definition in PIPELINE_STAGE_DEFINITIONS
                },
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
            "admission": self._build_admission_summary(latest_admission),
            "status": status,
            "started_at": started_at,
            "last_event_at": last_event_at,
            "event_count": event_count or len(timeline),
            "last_stage": last_stage,
            "reason": reason,
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
            stage = pipeline_stage_name(event_type)
            events.append(
                self._timeline_event(
                    event_id=(
                        f"{signal_id}:pipeline:{row.get('trace_id')}:{row.get('id') or self._datetime_key(row.get('recorded_at'))}:{event_type}"
                    ),
                    stage=stage,
                    status=pipeline_event_status(row),
                    at=row.get("recorded_at"),
                    source="pipeline_trace_events",
                    summary=pipeline_event_summary(row),
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

    def _build_trace_summary_item(self, row: dict[str, Any]) -> dict[str, Any]:
        last_event_type = str(row.get("last_event_type") or "")
        return {
            "trace_id": str(row.get("trace_id") or ""),
            "signal_id": self._str_or_none(row.get("signal_id")),
            "symbol": self._str_or_none(row.get("symbol")),
            "timeframe": self._str_or_none(row.get("timeframe")),
            "strategy": self._str_or_none(row.get("strategy")),
            "status": self._str_or_none(row.get("status")) or "in_progress",
            "started_at": self._normalize_datetime(row.get("started_at")),
            "last_event_at": self._normalize_datetime(row.get("last_event_at")),
            "event_count": int(row.get("event_count") or 0),
            "last_stage": pipeline_stage_name(last_event_type) if last_event_type else None,
            "reason": self._str_or_none(row.get("reason")),
            "admission": self._build_admission_summary(
                {
                    "decision": row.get("last_admission_decision"),
                    "stage": row.get("last_admission_stage"),
                    "trace_id": row.get("trace_id"),
                    "signal_id": row.get("signal_id"),
                }
            ),
        }

    def _derive_trace_status(
        self,
        *,
        timeline: Sequence[dict[str, Any]],
        pipeline_events: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        last_event = timeline[-1] if timeline else None
        last_stage = str(last_event.get("stage") or "") if last_event else None
        last_status = str(last_event.get("status") or "") if last_event else ""
        if last_status in {"failed", "blocked", "skipped"}:
            status = last_status
        elif last_status == "block":
            status = "blocked"
        elif last_stage == "pipeline.admission_report":
            status = "admission"
        elif last_stage in {"outcome.trade", "position.closed"}:
            status = "completed"
        elif last_stage in {"trade.execute_trade", "pipeline.execution_submitted", "pending.filled"}:
            status = "submitted"
        elif last_stage == "signal.confirmed":
            status = "signal_confirmed"
        else:
            status = "in_progress"
        return {
            "status": status,
            "started_at": timeline[0]["at"] if timeline else None,
            "last_event_at": last_event.get("at") if last_event else None,
            "event_count": len(timeline),
            "last_stage": last_stage,
            "reason": self._extract_reason(last_event, pipeline_events),
        }

    @staticmethod
    def _extract_reason(
        last_event: dict[str, Any] | None,
        pipeline_events: Sequence[dict[str, Any]],
    ) -> str | None:
        if last_event is not None:
            details = last_event.get("details") or {}
            if extracted := TradingFlowTraceReadModel._extract_reason_from_mapping(details):
                return extracted
        for row in reversed(list(pipeline_events)):
            payload = row.get("payload") or {}
            if extracted := TradingFlowTraceReadModel._extract_reason_from_mapping(payload):
                return extracted
        return None

    @staticmethod
    def _extract_reason_from_mapping(payload: dict[str, Any]) -> str | None:
        for key in ("reason", "skip_reason", "category", "skip_category"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        nested = payload.get("result")
        if isinstance(nested, dict):
            for key in ("reason", "skip_reason", "category", "skip_category"):
                value = str(nested.get(key) or "").strip()
                if value:
                    return value
        return None

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
    def _extract_admission_reports(
        pipeline_events: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        reports: list[dict[str, Any]] = []
        for row in pipeline_events:
            if str(row.get("event_type") or "").strip() != "admission_report_appended":
                continue
            payload = dict(row.get("payload") or {})
            reports.append(
                {
                    **payload,
                    "recorded_at": TradingFlowTraceReadModel._normalize_datetime(
                        row.get("recorded_at")
                    ),
                    "trace_id": str(row.get("trace_id") or payload.get("trace_id") or "").strip()
                    or None,
                    "signal_id": str(row.get("signal_id") or payload.get("signal_id") or "").strip()
                    or None,
                    "intent_id": str(row.get("intent_id") or payload.get("intent_id") or "").strip()
                    or None,
                    "command_id": str(row.get("command_id") or payload.get("command_id") or "").strip()
                    or None,
                    "action_id": str(row.get("action_id") or payload.get("action_id") or "").strip()
                    or None,
                }
            )
        return reports

    @staticmethod
    def _build_admission_summary(
        report: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(report, dict) or not report:
            return None
        reasons = list(report.get("reasons") or [])
        return {
            "decision": str(report.get("decision") or "").strip() or None,
            "stage": str(report.get("stage") or "").strip() or None,
            "generated_at": TradingFlowTraceReadModel._normalize_datetime(
                report.get("generated_at") or report.get("recorded_at")
            ),
            "reason_count": len(reasons),
            "trace_id": str(report.get("trace_id") or "").strip() or None,
            "signal_id": str(report.get("signal_id") or "").strip() or None,
            "intent_id": str(report.get("intent_id") or "").strip() or None,
            "command_id": str(report.get("command_id") or "").strip() or None,
            "action_id": str(report.get("action_id") or "").strip() or None,
        }

    @staticmethod
    def _fetch_by_signal_ids(
        *,
        signal_ids: Sequence[str],
        fetcher: Any,
        limit: int,
        account_alias: str | None = None,
    ) -> list[dict[str, Any]]:
        # §0v P2/P3：(a) account_alias 透传给 fetcher（None 时保留全局语义，
        # signal_outcomes 等不带账户字段的 fetcher 仍向后兼容）；(b) 去重键
        # 加 account 维度，避免两账户同时刻同 signal_id 事件被错误折叠成一条。
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for signal_id in signal_ids:
            kwargs: dict[str, Any] = {"signal_id": signal_id, "limit": limit}
            if account_alias is not None:
                kwargs["account_alias"] = account_alias
            for row in fetcher(**kwargs):
                key = (
                    str(row.get("signal_id") or ""),
                    str(row.get("recorded_at") or row.get("executed_at") or ""),
                    str(row.get("account_key") or ""),
                    str(row.get("account_alias") or ""),
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

    @staticmethod
    def _str_or_none(value: Any) -> Optional[str]:
        text = str(value or "").strip()
        return text or None
