from __future__ import annotations

from datetime import datetime, timedelta, timezone
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

    DEFAULT_TRACE_DIRECTORY_LOOKBACK_HOURS = 6

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
        sl_tp_history = self._fetch_sl_tp_history(positions=positions)
        recovery_cycles = self._fetch_recovery_cycles_by_signal_ids(
            signal_ids=[normalized_signal_id],
            limit=100,
        )
        operations = self._command_audit_repo.fetch_trace_operations(
            account_alias=self._account_alias_getter(),
            signal_id=normalized_signal_id,
            limit=100,
        )
        trace_ids = self._collect_trace_ids(
            preview_signals=[preview_signal] if preview_signal is not None else [],
            confirmed_signals=(
                [confirmed_signal] if confirmed_signal is not None else []
            ),
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
            confirmed_signals=(
                [confirmed_signal] if confirmed_signal is not None else []
            ),
            pipeline_events=pipeline_events,
            auto_executions=auto_executions,
            operations=operations,
            pending_orders=pending_orders,
            positions=positions,
            recovery_cycles=recovery_cycles,
            sl_tp_history=sl_tp_history,
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
        sl_tp_history = self._fetch_sl_tp_history(positions=positions)
        recovery_cycles = self._fetch_recovery_cycles_by_signal_ids(
            signal_ids=signal_ids,
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
            recovery_cycles=recovery_cycles,
            sl_tp_history=sl_tp_history,
            signal_outcomes=signal_outcomes,
            trade_outcomes=trade_outcomes,
        )

    def trace_by_intent_id(self, intent_id: str) -> dict[str, Any]:
        return self._trace_by_pipeline_identifier(
            identifier_name="intent_id",
            identifier_value=intent_id,
        )

    def trace_by_command_id(self, command_id: str) -> dict[str, Any]:
        return self._trace_by_pipeline_identifier(
            identifier_name="command_id",
            identifier_value=command_id,
        )

    def trace_by_action_id(self, action_id: str) -> dict[str, Any]:
        return self._trace_by_pipeline_identifier(
            identifier_name="action_id",
            identifier_value=action_id,
        )

    def _trace_by_pipeline_identifier(
        self,
        *,
        identifier_name: str,
        identifier_value: str,
    ) -> dict[str, Any]:
        normalized_value = str(identifier_value or "").strip()
        if not normalized_value:
            return self._build_trace_response(
                signal_id=None,
                trace_id=None,
                preview_signals=[],
                confirmed_signals=[],
                pipeline_events=[],
                auto_executions=[],
                operations=[],
                pending_orders=[],
                positions=[],
                recovery_cycles=[],
                sl_tp_history=[],
                signal_outcomes=[],
                trade_outcomes=[],
            )

        filtered_events = self._pipeline_trace_repo.fetch_pipeline_trace_filtered(
            **{identifier_name: normalized_value},
            limit=500,
        )
        identifier_operations = self._fetch_operations_by_pipeline_identifier(
            identifier_name=identifier_name,
            identifier_value=normalized_value,
            limit=100,
        )
        trace_ids = self._collect_trace_ids(
            preview_signals=[],
            confirmed_signals=[],
            operations=identifier_operations,
            pipeline_events=filtered_events,
        )
        pipeline_events = (
            self._pipeline_trace_repo.fetch_pipeline_trace_events(
                trace_ids=trace_ids,
                limit=500,
            )
            if trace_ids
            else filtered_events
        )
        preview_signals: list[dict[str, Any]] = []
        confirmed_signals: list[dict[str, Any]] = []
        operations: list[dict[str, Any]] = list(identifier_operations)
        for trace_id in trace_ids:
            preview_signals.extend(
                self._signal_repo.fetch_signal_events_by_trace_id(
                    trace_id=trace_id,
                    scope="preview",
                    limit=50,
                )
            )
            confirmed_signals.extend(
                self._signal_repo.fetch_signal_events_by_trace_id(
                    trace_id=trace_id,
                    scope="confirmed",
                    limit=50,
                )
            )
            operations.extend(
                self._command_audit_repo.fetch_trace_operations_by_trace_id(
                    account_alias=self._account_alias_getter(),
                    trace_id=trace_id,
                    limit=100,
                )
            )
        operations = self._dedupe_operations(operations)

        signal_ids = self._collect_signal_ids(
            preview_signals=preview_signals,
            confirmed_signals=confirmed_signals,
            operations=operations,
            pipeline_events=pipeline_events,
        )
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
        sl_tp_history = self._fetch_sl_tp_history(positions=positions)
        recovery_cycles = self._fetch_recovery_cycles_by_signal_ids(
            signal_ids=signal_ids,
            limit=100,
        )
        primary_signal_id = signal_ids[0] if signal_ids else None
        primary_trace_id = trace_ids[0] if len(trace_ids) == 1 else None
        return self._build_trace_response(
            signal_id=primary_signal_id,
            trace_id=primary_trace_id,
            preview_signals=preview_signals,
            confirmed_signals=confirmed_signals,
            pipeline_events=pipeline_events,
            auto_executions=auto_executions,
            operations=operations,
            pending_orders=pending_orders,
            positions=positions,
            recovery_cycles=recovery_cycles,
            sl_tp_history=sl_tp_history,
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
        intent_id: str | None = None,
        command_id: str | None = None,
        action_id: str | None = None,
        status: str | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        page: int = 1,
        page_size: int = 100,
        sort: str = "last_event_at_desc",
    ) -> dict[str, Any]:
        effective_from_time = from_time
        default_window_applied = False
        has_precise_identifier = any(
            str(item or "").strip()
            for item in (trace_id, signal_id, intent_id, command_id, action_id)
        )
        if (
            effective_from_time is None
            and to_time is None
            and not has_precise_identifier
        ):
            effective_from_time = datetime.now(timezone.utc) - timedelta(
                hours=self.DEFAULT_TRACE_DIRECTORY_LOOKBACK_HOURS
            )
            default_window_applied = True
        query_result = self._pipeline_trace_repo.query_trace_summaries(
            trace_id=trace_id,
            signal_id=signal_id,
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            intent_id=intent_id,
            command_id=command_id,
            action_id=action_id,
            status=status,
            from_time=effective_from_time,
            to_time=to_time,
            page=page,
            page_size=page_size,
            sort=sort,
        )
        items = [
            self._build_trace_summary_item(row) for row in query_result.get("items", [])
        ]
        return {
            "items": items,
            "total": int(query_result.get("total") or 0),
            "page": int(query_result.get("page") or page),
            "page_size": int(query_result.get("page_size") or page_size),
            "from_time": self._normalize_datetime(effective_from_time),
            "to_time": self._normalize_datetime(to_time),
            "default_window_applied": default_window_applied,
        }

    def _fetch_operations_by_pipeline_identifier(
        self,
        *,
        identifier_name: str,
        identifier_value: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if identifier_name == "action_id" and hasattr(
            self._command_audit_repo,
            "fetch_trace_operations_by_action_id",
        ):
            return list(
                self._command_audit_repo.fetch_trace_operations_by_action_id(
                    account_alias=self._account_alias_getter(),
                    action_id=identifier_value,
                    limit=limit,
                )
            )
        if identifier_name == "command_id" and hasattr(
            self._command_audit_repo,
            "fetch_trace_operations_by_command_id",
        ):
            return list(
                self._command_audit_repo.fetch_trace_operations_by_command_id(
                    account_alias=self._account_alias_getter(),
                    command_id=identifier_value,
                    limit=limit,
                )
            )
        return []

    @staticmethod
    def _dedupe_operations(
        operations: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in operations:
            operation_id = str(row.get("operation_id") or "").strip()
            key = operation_id or (
                f"{row.get('recorded_at')}:{row.get('command_type')}:{row.get('ticket')}"
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(dict(row))
        return deduped

    def _fetch_sl_tp_history(
        self,
        *,
        positions: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        fetcher = getattr(
            self._trading_state_repo, "fetch_position_sl_tp_history", None
        )
        if not callable(fetcher):
            return []
        tickets = sorted(
            {
                ticket
                for ticket in (
                    self._int_or_none(row.get("position_ticket") or row.get("ticket"))
                    for row in positions
                )
                if ticket is not None
            }
        )
        if not tickets:
            return []
        rows: list[dict[str, Any]] = []
        seen: set[tuple[int, str, str]] = set()
        for row in fetcher(
            account_alias=self._account_alias_getter(),
            position_tickets=tickets,
            limit=500,
        ):
            ticket = self._int_or_none(row.get("position_ticket"))
            key = (
                int(ticket or 0),
                str(row.get("recorded_at") or ""),
                str(row.get("action_type") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(dict(row))
        rows.sort(key=lambda item: str(item.get("recorded_at") or ""))
        return rows

    def _fetch_recovery_cycles_by_signal_ids(
        self,
        *,
        signal_ids: Sequence[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        fetcher = getattr(self._trading_state_repo, "fetch_recovery_cycle_states", None)
        if not callable(fetcher):
            return []
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for signal_id in signal_ids:
            normalized_signal_id = str(signal_id or "").strip()
            if not normalized_signal_id:
                continue
            for row in fetcher(
                account_alias=self._account_alias_getter(),
                source_signal_id=normalized_signal_id,
                limit=limit,
            ):
                account_key = str(row.get("account_key") or "").strip()
                cycle_id = str(row.get("cycle_id") or "").strip()
                key = (account_key, cycle_id)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(dict(row))
        rows.sort(key=lambda item: str(item.get("updated_at") or ""))
        return rows

    def _build_lifecycle_projection(
        self,
        *,
        timeline: Sequence[dict[str, Any]],
        operations: Sequence[dict[str, Any]],
        pending_orders: Sequence[dict[str, Any]],
        positions: Sequence[dict[str, Any]],
        sl_tp_history: Sequence[dict[str, Any]],
        trade_outcomes: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        pending = self._latest_by_time(pending_orders, self._pending_event_time)
        position = self._latest_by_time(
            positions,
            lambda row: row.get("closed_at")
            or row.get("last_seen_at")
            or row.get("opened_at")
            or row.get("updated_at"),
        )
        entry_operation = self._first_operation_of_type(operations, "execute_trade")
        close_commands = [
            dict(row)
            for row in operations
            if str(row.get("command_type") or "").startswith("close")
        ]
        outcome = self._latest_by_time(
            trade_outcomes, lambda row: row.get("recorded_at")
        )

        order_ticket = self._first_int(
            pending.get("order_ticket") if pending else None,
            position.get("order_ticket") if position else None,
            entry_operation.get("ticket") if entry_operation else None,
            entry_operation.get("order_id") if entry_operation else None,
        )
        position_ticket = self._first_int(
            position.get("position_ticket") if position else None,
            position.get("ticket") if position else None,
            pending.get("position_ticket") if pending else None,
            self._extract_int_from_payloads(close_commands, "ticket"),
            self._extract_int_from_payloads(close_commands, "position_ticket"),
        )
        position_status = str((position or {}).get("status") or "").strip()
        closed_at = self._normalize_datetime(
            (position or {}).get("closed_at")
            or (close_commands[-1].get("recorded_at") if close_commands else None)
        )
        opened_at = self._normalize_datetime(
            (position or {}).get("opened_at")
            or (pending or {}).get("filled_at")
            or (entry_operation or {}).get("recorded_at")
        )
        lifecycle_status = self._derive_lifecycle_status(
            position_status=position_status,
            pending_status=str((pending or {}).get("status") or ""),
            close_commands=close_commands,
            outcome=outcome,
        )

        sl_tp_updates = [self._json_safe(dict(row)) for row in sl_tp_history]
        latest_sl_tp = sl_tp_history[-1] if sl_tp_history else {}
        latest_stop_loss = (
            latest_sl_tp.get("new_stop_loss")
            if latest_sl_tp
            else (position or {}).get("current_stop_loss")
        )
        latest_take_profit = (
            latest_sl_tp.get("new_take_profit")
            if latest_sl_tp
            else (position or {}).get("current_take_profit")
        )

        data_gaps: list[str] = []
        if not pending and not entry_operation and not position:
            data_gaps.append("entry_fact_missing")
        if position_status == "closed" and not closed_at:
            data_gaps.append("closed_time_missing")

        lifecycle_timeline = self._build_lifecycle_timeline(
            entry_operation=entry_operation,
            pending=pending,
            position=position,
            sl_tp_history=sl_tp_history,
            close_commands=close_commands,
            outcome=outcome,
        )

        return {
            "summary": {
                "status": lifecycle_status,
                "order_ticket": order_ticket,
                "position_ticket": position_ticket,
                "opened_at": opened_at,
                "closed_at": closed_at,
                "duration_seconds": self._duration_seconds(opened_at, closed_at),
                "last_stage": (
                    lifecycle_timeline[-1]["stage"] if lifecycle_timeline else None
                ),
                "trace_event_count": len(timeline),
            },
            "entry": {
                "status": str((pending or {}).get("status") or "submitted"),
                "order_ticket": order_ticket,
                "position_ticket": position_ticket,
                "filled_at": self._normalize_datetime((pending or {}).get("filled_at")),
                "fill_price": (pending or {}).get("fill_price")
                or (position or {}).get("entry_price")
                or (position or {}).get("open_price"),
                "operation": self._json_safe(entry_operation),
                "pending_order": self._json_safe(pending),
            },
            "management": {
                "update_count": len(sl_tp_updates),
                "latest_stop_loss": latest_stop_loss,
                "latest_take_profit": latest_take_profit,
                "sl_tp_updates": sl_tp_updates,
            },
            "exit": {
                "status": position_status or ("closed" if close_commands else None),
                "closed_at": closed_at,
                "close_price": (position or {}).get("close_price"),
                "close_source": (position or {}).get("close_source"),
                "close_commands": self._json_safe(close_commands),
            },
            "outcome": self._json_safe(outcome),
            "timeline": lifecycle_timeline,
            "data_gaps": data_gaps,
        }

    def _build_lifecycle_timeline(
        self,
        *,
        entry_operation: dict[str, Any] | None,
        pending: dict[str, Any] | None,
        position: dict[str, Any] | None,
        sl_tp_history: Sequence[dict[str, Any]],
        close_commands: Sequence[dict[str, Any]],
        outcome: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        entry_time = (
            (pending or {}).get("filled_at")
            or (position or {}).get("opened_at")
            or (entry_operation or {}).get("recorded_at")
        )
        if entry_time is not None:
            events.append(
                {
                    "stage": "entry",
                    "status": str((pending or {}).get("status") or "submitted"),
                    "at": self._normalize_datetime(entry_time),
                    "source": (
                        "pending_order_states" if pending else "trade_command_audits"
                    ),
                    "details": self._json_safe(pending or entry_operation or {}),
                }
            )
        for row in sl_tp_history:
            events.append(
                {
                    "stage": "management",
                    "status": "success" if row.get("success") is True else "failed",
                    "at": self._normalize_datetime(row.get("recorded_at")),
                    "source": "position_sl_tp_history",
                    "details": self._json_safe(row),
                }
            )
        for row in close_commands:
            events.append(
                {
                    "stage": "exit",
                    "status": str(row.get("status") or "unknown"),
                    "at": self._normalize_datetime(row.get("recorded_at")),
                    "source": "trade_command_audits",
                    "details": self._json_safe(row),
                }
            )
        if position is not None and str(position.get("status") or "") == "closed":
            events.append(
                {
                    "stage": "exit",
                    "status": "closed",
                    "at": self._normalize_datetime(position.get("closed_at")),
                    "source": "position_runtime_states",
                    "details": self._json_safe(position),
                }
            )
        if outcome is not None:
            events.append(
                {
                    "stage": "outcome",
                    "status": (
                        "won"
                        if outcome.get("won") is True
                        else "lost" if outcome.get("won") is False else "unknown"
                    ),
                    "at": self._normalize_datetime(outcome.get("recorded_at")),
                    "source": "trade_outcomes",
                    "details": self._json_safe(outcome),
                }
            )
        events.sort(
            key=lambda item: (
                1 if item["at"] is None else 0,
                item["at"] or "",
                item["stage"],
            )
        )
        return events

    @staticmethod
    def _derive_lifecycle_status(
        *,
        position_status: str,
        pending_status: str,
        close_commands: Sequence[dict[str, Any]],
        outcome: dict[str, Any] | None,
    ) -> str:
        normalized_position = position_status.lower()
        if normalized_position == "closed" or outcome is not None:
            return "closed"
        if close_commands and str(close_commands[-1].get("status") or "") == "success":
            return "closed"
        if normalized_position in {"open", "active"}:
            return "open"
        if pending_status.lower() == "filled":
            return "open"
        if pending_status:
            return pending_status.lower()
        return "unknown"

    @staticmethod
    def _latest_by_time(
        rows: Sequence[dict[str, Any]],
        time_getter: Callable[[dict[str, Any]], Any],
    ) -> dict[str, Any] | None:
        if not rows:
            return None
        return max(rows, key=lambda row: str(time_getter(row) or ""))

    @staticmethod
    def _first_operation_of_type(
        operations: Sequence[dict[str, Any]],
        command_type: str,
    ) -> dict[str, Any] | None:
        for row in operations:
            if str(row.get("command_type") or "") == command_type:
                return dict(row)
        return None

    def _extract_int_from_payloads(
        self,
        rows: Sequence[dict[str, Any]],
        key: str,
    ) -> int | None:
        for row in rows:
            for payload in (
                row,
                row.get("request_payload") or {},
                row.get("response_payload") or {},
            ):
                value = self._int_or_none(payload.get(key))
                if value is not None:
                    return value
        return None

    def _first_int(self, *values: Any) -> int | None:
        for value in values:
            normalized = self._int_or_none(value)
            if normalized is not None:
                return normalized
        return None

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
        recovery_cycles: Sequence[dict[str, Any]],
        sl_tp_history: Sequence[dict[str, Any]],
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
            pipeline_events=pipeline_events,
        )
        signal_ids = self._collect_signal_ids(
            preview_signals=preview_signals,
            confirmed_signals=confirmed_signals,
            operations=operations,
            pipeline_events=pipeline_events,
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
            "recovery_cycles": list(recovery_cycles),
            "position_sl_tp_history": list(sl_tp_history),
            "signal_outcomes": list(signal_outcomes),
            "trade_outcomes": list(trade_outcomes),
        }
        identifiers = self._build_identifiers(
            signal_id=normalized_signal_id,
            signal_ids=signal_ids,
            trace_ids=trace_ids,
            pipeline_events=pipeline_events,
            operations=operations,
            pending_orders=pending_orders,
            positions=positions,
            recovery_cycles=recovery_cycles,
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
            recovery_cycles=recovery_cycles,
            signal_outcomes=signal_outcomes,
            trade_outcomes=trade_outcomes,
        )
        lifecycle = self._build_lifecycle_projection(
            timeline=timeline,
            operations=operations,
            pending_orders=pending_orders,
            positions=positions,
            sl_tp_history=sl_tp_history,
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
                    recovery_cycles,
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
            "lifecycle": lifecycle,
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
        recovery_cycles = list(facts.get("recovery_cycles") or [])
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
                "preview_signal": (
                    "present" if facts.get("signal_preview") else "missing"
                ),
                "confirmed_signal": (
                    "present" if facts.get("signal_confirmed") else "missing"
                ),
                "auto_execution": (
                    "present" if facts.get("auto_executions") else "missing"
                ),
                "trade_command_audit": "present" if operations else "missing",
                "pending_order": "present" if pending_orders else "missing",
                "position_runtime": "present" if positions else "missing",
                "recovery_cycle": "present" if recovery_cycles else "missing",
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
            "recovery_cycle_status_counts": self._count_by_key(
                recovery_cycles,
                "status",
            ),
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
        pipeline_events: Sequence[dict[str, Any]],
        operations: Sequence[dict[str, Any]],
        trace_ids: Sequence[str],
        pending_orders: Sequence[dict[str, Any]],
        positions: Sequence[dict[str, Any]],
        recovery_cycles: Sequence[dict[str, Any]],
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
        intent_ids: set[str] = set()
        command_ids: set[str] = set()
        action_ids: set[str] = set()
        account_keys: set[str] = set()
        account_aliases: set[str] = set()
        order_tickets: set[int] = set()
        position_tickets: set[int] = set()
        recovery_cycle_ids: set[str] = set()

        for row in pipeline_events:
            payload = row.get("payload") or {}
            for candidate in (
                row.get("trace_id"),
                payload.get("trace_id"),
            ):
                value = str(candidate or "").strip()
                if value:
                    normalized_trace_ids.add(value)
            for candidate in (
                row.get("signal_id"),
                payload.get("signal_id"),
                payload.get("request_id"),
            ):
                value = str(candidate or "").strip()
                if value:
                    request_ids.add(value)
            for target, candidates in (
                (intent_ids, (row.get("intent_id"), payload.get("intent_id"))),
                (command_ids, (row.get("command_id"), payload.get("command_id"))),
                (action_ids, (row.get("action_id"), payload.get("action_id"))),
                (account_keys, (row.get("account_key"), payload.get("account_key"))),
                (
                    account_aliases,
                    (row.get("account_alias"), payload.get("account_alias")),
                ),
            ):
                for candidate in candidates:
                    value = str(candidate or "").strip()
                    if value:
                        target.add(value)

        for row in operations:
            operation_id = str(row.get("operation_id") or "").strip()
            if operation_id:
                operation_ids.add(operation_id)
            request_payload = row.get("request_payload") or {}
            response_payload = row.get("response_payload") or {}
            for target, keys in (
                (intent_ids, ("intent_id",)),
                (command_ids, ("command_id",)),
                (action_ids, ("action_id",)),
                (account_keys, ("account_key",)),
                (account_aliases, ("account_alias",)),
            ):
                for payload in (request_payload, response_payload):
                    for key in keys:
                        value = str(payload.get(key) or row.get(key) or "").strip()
                        if value:
                            target.add(value)
            for candidate in (
                request_payload.get("request_id"),
                response_payload.get("request_id"),
                response_payload.get("trace_id"),
            ):
                value = str(candidate or "").strip()
                if value:
                    request_ids.add(value)
            for payload in (row, request_payload, response_payload):
                for key in ("ticket", "order_ticket", "order", "order_id"):
                    ticket = self._int_or_none(payload.get(key))
                    if ticket:
                        order_tickets.add(ticket)
                for key in ("position", "position_ticket"):
                    position_ticket = self._int_or_none(payload.get(key))
                    if position_ticket:
                        position_tickets.add(position_ticket)

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

        for row in recovery_cycles:
            cycle_id = str(row.get("cycle_id") or "").strip()
            if cycle_id:
                recovery_cycle_ids.add(cycle_id)
            for target, candidates in (
                (account_keys, (row.get("account_key"),)),
                (account_aliases, (row.get("account_alias"),)),
                (request_ids, (row.get("source_signal_id"),)),
            ):
                for candidate in candidates:
                    value = str(candidate or "").strip()
                    if value:
                        target.add(value)

        return {
            "signal_id": signal_id,
            "signal_ids": sorted(
                {str(item).strip() for item in signal_ids if str(item or "").strip()}
            ),
            "request_ids": sorted(request_ids),
            "trace_ids": sorted(normalized_trace_ids),
            "operation_ids": sorted(operation_ids),
            "intent_ids": sorted(intent_ids),
            "command_ids": sorted(command_ids),
            "action_ids": sorted(action_ids),
            "account_keys": sorted(account_keys),
            "account_aliases": sorted(account_aliases),
            "order_tickets": sorted(order_tickets),
            "position_tickets": sorted(position_tickets),
            "recovery_cycle_ids": sorted(recovery_cycle_ids),
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
        recovery_cycles: Sequence[dict[str, Any]],
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
                    at=row.get("closed_at")
                    or row.get("opened_at")
                    or row.get("updated_at"),
                    source="position_runtime_states",
                    summary=f"持仓状态: {status}",
                    details=row,
                )
            )
        for row in recovery_cycles:
            status = str(row.get("status") or "unknown")
            events.append(
                self._timeline_event(
                    event_id=f"{signal_id}:recovery:{row.get('cycle_id')}:{status}",
                    stage=f"recovery.{status}",
                    status=status,
                    at=row.get("updated_at")
                    or row.get("last_step_at")
                    or row.get("started_at"),
                    source="recovery_cycle_states",
                    summary=f"恢复周期状态: {status}",
                    details=row,
                )
            )
        for row in signal_outcomes:
            events.append(
                self._timeline_event(
                    event_id=f"{signal_id}:signal_outcome:{self._datetime_key(row.get('recorded_at'))}",
                    stage="outcome.signal",
                    status=(
                        "won"
                        if row.get("won") is True
                        else "lost" if row.get("won") is False else "unknown"
                    ),
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
                    status=(
                        "won"
                        if row.get("won") is True
                        else "lost" if row.get("won") is False else "unknown"
                    ),
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
            "intent_id": self._str_or_none(row.get("intent_id")),
            "command_id": self._str_or_none(row.get("command_id")),
            "action_id": self._str_or_none(row.get("action_id")),
            "status": self._str_or_none(row.get("status")) or "in_progress",
            "started_at": self._normalize_datetime(row.get("started_at")),
            "last_event_at": self._normalize_datetime(row.get("last_event_at")),
            "event_count": int(row.get("event_count") or 0),
            "last_stage": (
                pipeline_stage_name(last_event_type) if last_event_type else None
            ),
            "reason": self._str_or_none(row.get("reason")),
            "admission": self._build_admission_summary(
                {
                    "decision": row.get("last_admission_decision"),
                    "stage": row.get("last_admission_stage"),
                    "trace_id": row.get("trace_id"),
                    "signal_id": row.get("signal_id"),
                    "intent_id": row.get("intent_id"),
                    "command_id": row.get("command_id"),
                    "action_id": row.get("action_id"),
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
        elif last_stage in {"outcome.trade", "position.closed", "recovery.closed"}:
            status = "completed"
        elif (
            last_stage
            in {
                "trade.close_position",
                "trade.close_all_positions",
                "trade.close_positions_by_tickets",
                "trade.cancel_orders",
                "trade.cancel_orders_by_tickets",
                "trade.close_exposure",
            }
            and last_status == "success"
        ):
            status = "completed"
        elif last_stage in {
            "trade.execute_trade",
            "pipeline.execution_submitted",
            "pending.filled",
        }:
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
            if extracted := TradingFlowTraceReadModel._extract_reason_from_mapping(
                details
            ):
                return extracted
        for row in reversed(list(pipeline_events)):
            payload = row.get("payload") or {}
            if extracted := TradingFlowTraceReadModel._extract_reason_from_mapping(
                payload
            ):
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
        pipeline_events: Sequence[dict[str, Any]] = (),
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
            for payload in (
                row.get("request_payload") or {},
                row.get("response_payload") or {},
            ):
                trace_id = str(payload.get("trace_id") or "").strip()
                if trace_id:
                    trace_ids.add(trace_id)
        for row in pipeline_events:
            payload = row.get("payload") or {}
            for candidate in (row.get("trace_id"), payload.get("trace_id")):
                trace_id = str(candidate or "").strip()
                if trace_id:
                    trace_ids.add(trace_id)
        return sorted(trace_ids)

    def _collect_signal_ids(
        self,
        *,
        preview_signals: Sequence[dict[str, Any]],
        confirmed_signals: Sequence[dict[str, Any]],
        operations: Sequence[dict[str, Any]],
        pipeline_events: Sequence[dict[str, Any]] = (),
    ) -> list[str]:
        signal_ids: set[str] = set()
        for row in [*preview_signals, *confirmed_signals]:
            signal_id = str(row.get("signal_id") or "").strip()
            if signal_id:
                signal_ids.add(signal_id)
        for row in operations:
            for payload in (
                row.get("request_payload") or {},
                row.get("response_payload") or {},
            ):
                metadata = payload.get("metadata") or {}
                signal = metadata.get("signal") or {}
                for candidate in (
                    signal.get("signal_id"),
                    metadata.get("source_signal_id"),
                    payload.get("signal_id"),
                ):
                    signal_id = str(candidate or "").strip()
                    if signal_id:
                        signal_ids.add(signal_id)
        for row in pipeline_events:
            payload = row.get("payload") or {}
            for candidate in (
                row.get("signal_id"),
                payload.get("signal_id"),
                payload.get("request_id"),
            ):
                signal_id = str(candidate or "").strip()
                if signal_id:
                    signal_ids.add(signal_id)
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
                    "trace_id": str(
                        row.get("trace_id") or payload.get("trace_id") or ""
                    ).strip()
                    or None,
                    "signal_id": str(
                        row.get("signal_id") or payload.get("signal_id") or ""
                    ).strip()
                    or None,
                    "intent_id": str(
                        row.get("intent_id") or payload.get("intent_id") or ""
                    ).strip()
                    or None,
                    "command_id": str(
                        row.get("command_id") or payload.get("command_id") or ""
                    ).strip()
                    or None,
                    "action_id": str(
                        row.get("action_id") or payload.get("action_id") or ""
                    ).strip()
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
    def _duration_seconds(start: str | None, end: str | None) -> float | None:
        if not start or not end:
            return None
        try:
            start_dt = datetime.fromisoformat(str(start))
            end_dt = datetime.fromisoformat(str(end))
        except (TypeError, ValueError):
            return None
        return max(0.0, (end_dt - start_dt).total_seconds())

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
