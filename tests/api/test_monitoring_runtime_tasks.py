from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from src.api import deps
from src.api.monitoring import (
    cancel_pending_entries_by_symbol,
    cancel_pending_entry,
    get_pending_entries,
    get_runtime_tasks,
)
from src.api.schemas import (
    PendingEntriesBySymbolCancelRequest,
    PendingEntryCancelRequest,
)
from src.trading.application.idempotency import TradeOperatorActionReplayConflictError


class _ActionCommandService:
    def __init__(self) -> None:
        self.last_recorded_action = None
        self._replays = {}

    def find_operator_action_replay(self, **kwargs):
        command_type = str(kwargs.get("command_type") or "").strip()
        idempotency_key = str(kwargs.get("idempotency_key") or "").strip()
        if not command_type or not idempotency_key:
            return None
        replay = self._replays.get((command_type, idempotency_key))
        if replay is None:
            return None
        request_payload = self._normalize_payload(kwargs.get("request_payload") or {})
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

    def record_operator_action(self, **kwargs):
        self.last_recorded_action = dict(kwargs)
        operation_id = kwargs.get("operation_id") or "act_runtime_1"
        request_payload = dict(kwargs.get("request_payload") or {})
        response_payload = dict(kwargs.get("response_payload") or {})
        response_payload.setdefault("action_id", operation_id)
        response_payload.setdefault("audit_id", operation_id)
        response_payload.setdefault("recorded_at", "2026-01-01T00:00:00+00:00")
        idempotency_key = str(request_payload.get("idempotency_key") or "").strip()
        command_type = str(kwargs.get("command_type") or "").strip()
        if command_type and idempotency_key:
            self._replays[(command_type, idempotency_key)] = {
                "request_payload": self._normalize_payload(request_payload),
                "response_payload": response_payload,
            }
        return {
            "operation_id": operation_id,
            "recorded_at": "2026-01-01T00:00:00+00:00",
            "status": kwargs.get("status") or "success",
        }

    @classmethod
    def _normalize_payload(cls, value):
        if isinstance(value, dict):
            return {
                str(key): cls._normalize_payload(item)
                for key, item in value.items()
                if str(key) not in {"action_id", "audit_id"}
            }
        if isinstance(value, list):
            return [cls._normalize_payload(item) for item in value]
        return value


class _PendingRuntimeFacade:
    def __init__(self) -> None:
        self.entries = {
            "sig_1": {"signal_id": "sig_1", "symbol": "XAUUSD"},
            "sig_2": {"signal_id": "sig_2", "symbol": "XAUUSD"},
        }
        self.cancel_calls = 0
        self.cancel_by_symbol_calls = 0

    def pending_entries_summary(self):
        return {
            "status": "healthy",
            "active_count": len(self.entries),
            "entries": list(self.entries.values()),
        }

    def cancel(self, signal_id: str, reason: str = "api") -> bool:
        self.cancel_calls += 1
        return self.entries.pop(signal_id, None) is not None

    def cancel_by_symbol(self, symbol: str, reason: str = "api") -> int:
        self.cancel_by_symbol_calls += 1
        matched = [key for key, item in self.entries.items() if item["symbol"] == symbol]
        for key in matched:
            self.entries.pop(key, None)
        return len(matched)


def test_get_runtime_task_status_formats_database_rows(monkeypatch) -> None:
    row = (
        "startup",
        "monitoring",
        datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        "ready",
        datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
        123,
        1,
        0,
        0,
        None,
        {"startup": True},
    )

    monkeypatch.setattr(deps, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(
        deps,
        "_container",
        SimpleNamespace(
            runtime_identity=SimpleNamespace(
                instance_id="main-live-run-001",
                instance_role="main",
                account_key="live:broker-live:1001",
                account_alias="live_main",
            ),
            storage_writer=SimpleNamespace(
                db=SimpleNamespace(
                    fetch_runtime_task_status_records=lambda **kwargs: [
                        {
                            "component": row[0],
                            "task_name": row[1],
                            "updated_at": row[2],
                            "state": row[3],
                            "started_at": row[4],
                            "completed_at": row[5],
                            "next_run_at": row[6],
                            "duration_ms": row[7],
                            "success_count": row[8],
                            "failure_count": row[9],
                            "consecutive_failures": row[10],
                            "last_error": row[11],
                            "details": row[12],
                            "instance_id": kwargs.get("instance_id"),
                            "instance_role": kwargs.get("instance_role"),
                            "account_key": kwargs.get("account_key"),
                            "account_alias": kwargs.get("account_alias"),
                        }
                    ]
                )
            )
        ),
    )

    items = deps.get_runtime_task_status(component="startup", task_name="monitoring")

    assert items == [
        {
            "component": "startup",
            "task_name": "monitoring",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "state": "ready",
            "started_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:01:00+00:00",
            "next_run_at": "2026-01-01T01:00:00+00:00",
            "duration_ms": 123,
            "success_count": 1,
            "failure_count": 0,
            "consecutive_failures": 0,
            "last_error": None,
            "details": {"startup": True},
            "instance_id": "main-live-run-001",
            "instance_role": None,
            "account_key": None,
            "account_alias": None,
        }
    ]


def test_resolve_runtime_task_scope_defaults_to_current_instance(monkeypatch) -> None:
    monkeypatch.setattr(deps, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(
        deps,
        "_container",
        SimpleNamespace(
            runtime_identity=SimpleNamespace(
                instance_id="exec-live-run-001",
                instance_role="executor",
                account_key="live:broker-live:1002",
                account_alias="live_exec_a",
            )
        ),
    )

    scope = deps.resolve_runtime_task_scope()

    assert scope == {
        "instance_id": "exec-live-run-001",
        "instance_role": None,
        "account_key": None,
        "account_alias": None,
    }


def test_resolve_runtime_task_scope_preserves_explicit_filters(monkeypatch) -> None:
    monkeypatch.setattr(deps, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(
        deps,
        "_container",
        SimpleNamespace(
            runtime_identity=SimpleNamespace(
                instance_id="main-live-run-001",
                instance_role="main",
                account_key="live:broker-live:1001",
                account_alias="live_main",
            )
        ),
    )

    scope = deps.resolve_runtime_task_scope(account_alias="live_exec_a")

    assert scope == {
        "instance_id": None,
        "instance_role": None,
        "account_key": None,
        "account_alias": "live_exec_a",
    }


def test_runtime_tasks_endpoint_returns_items_and_filters(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.api.monitoring_routes.runtime.resolve_runtime_task_scope",
        lambda **kwargs: {
            "instance_id": "exec-live-run-001",
            "instance_role": None,
            "account_key": None,
            "account_alias": None,
        },
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.runtime.get_runtime_task_status",
        lambda component=None, task_name=None, instance_id=None, instance_role=None, account_key=None, account_alias=None: [
            {
                "component": component,
                "task_name": task_name,
                "state": "ready",
                "details": {"startup": True},
                "instance_id": instance_id,
                "instance_role": instance_role,
                "account_key": account_key,
                "account_alias": account_alias,
            }
        ],
    )

    response = asyncio.run(get_runtime_tasks(component="startup", task_name="monitoring"))

    assert response.success is True
    assert response.data["items"] == [
        {
                "component": "startup",
                "task_name": "monitoring",
                "state": "ready",
                "details": {"startup": True},
                "instance_id": "exec-live-run-001",
                "instance_role": None,
                "account_key": None,
                "account_alias": None,
            }
        ]
    assert response.data["filters"] == {
        "component": "startup",
        "task_name": "monitoring",
        "instance_id": "exec-live-run-001",
        "instance_role": None,
        "account_key": None,
        "account_alias": None,
    }


def test_runtime_tasks_endpoint_explicit_scope_overrides_current_instance(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.api.monitoring_routes.runtime.resolve_runtime_task_scope",
        lambda **kwargs: {
            "instance_id": None,
            "instance_role": "executor",
            "account_key": None,
            "account_alias": "live_exec_a",
        },
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.runtime.get_runtime_task_status",
        lambda component=None, task_name=None, instance_id=None, instance_role=None, account_key=None, account_alias=None: [
            {
                "component": component,
                "task_name": task_name,
                "state": "ready",
                "instance_id": "main-live-main-f2356b80e0ab",
                "instance_role": instance_role,
                "account_key": "live:broker-live:1001",
                "account_alias": account_alias,
            }
        ],
    )

    response = asyncio.run(
        get_runtime_tasks(
            component="economic_calendar",
            task_name="release_watch",
            account_alias="live_exec_a",
        )
    )

    assert response.success is True
    assert response.data["items"] == [
        {
                "component": "economic_calendar",
                "task_name": "release_watch",
                "state": "ready",
                "instance_id": "main-live-main-f2356b80e0ab",
                "instance_role": "executor",
                "account_key": "live:broker-live:1001",
                "account_alias": "live_exec_a",
            }
        ]
    assert response.data["filters"]["instance_id"] is None
    assert response.data["filters"]["account_alias"] == "live_exec_a"


def test_pending_entries_endpoint_uses_runtime_read_model(monkeypatch) -> None:
    class _RuntimeViews:
        def pending_entries_summary(self):
            return {"status": "healthy", "active_count": 1, "entries": [{"signal_id": "sig_1"}]}

    monkeypatch.setattr("src.api.monitoring_routes.runtime.get_runtime_read_model", lambda: _RuntimeViews())

    response = asyncio.run(get_pending_entries())

    assert response.success is True
    assert response.data["active_count"] == 1
    assert response.data["entries"][0]["signal_id"] == "sig_1"


def test_cancel_pending_entry_returns_action_result_and_replays_same_idempotency_key() -> None:
    runtime = _PendingRuntimeFacade()
    command_service = _ActionCommandService()
    request = PendingEntryCancelRequest(
        reason="operator_cancel",
        actor="operator",
        idempotency_key="idem_pending_cancel_1",
        request_context={"panel": "pending_entries"},
    )

    first = asyncio.run(
        cancel_pending_entry(
            "sig_1",
            request,
            pending_entry_manager=runtime,
            command_service=command_service,
            runtime_views=runtime,
        )
    )
    second = asyncio.run(
        cancel_pending_entry(
            "sig_1",
            request,
            pending_entry_manager=runtime,
            command_service=command_service,
            runtime_views=runtime,
        )
    )

    assert first.success is True
    assert first.data["accepted"] is True
    assert first.data["status"] == "completed"
    assert first.data["signal_id"] == "sig_1"
    assert first.data["cancelled"] is True
    assert second.success is True
    assert second.metadata["replayed"] is True
    assert second.data["action_id"] == first.data["action_id"]
    assert runtime.cancel_calls == 1


def test_cancel_pending_entries_by_symbol_rejects_conflicting_idempotency_reuse() -> None:
    runtime = _PendingRuntimeFacade()
    command_service = _ActionCommandService()

    first = asyncio.run(
        cancel_pending_entries_by_symbol(
            PendingEntriesBySymbolCancelRequest(
                symbol="XAUUSD",
                reason="operator_cancel",
                actor="operator",
                idempotency_key="idem_pending_cancel_symbol_conflict",
                request_context={"panel": "pending_entries"},
            ),
            pending_entry_manager=runtime,
            command_service=command_service,
            runtime_views=runtime,
        )
    )
    second = asyncio.run(
        cancel_pending_entries_by_symbol(
            PendingEntriesBySymbolCancelRequest(
                symbol="EURUSD",
                reason="operator_cancel",
                actor="operator",
                idempotency_key="idem_pending_cancel_symbol_conflict",
                request_context={"panel": "pending_entries"},
            ),
            pending_entry_manager=runtime,
            command_service=command_service,
            runtime_views=runtime,
        )
    )

    assert first.success is True
    assert first.data["accepted"] is True
    assert first.data["cancelled_count"] == 2
    assert second.success is False
    assert second.error["code"] == "invalid_request"
    assert second.error["details"]["existing_action_id"] == first.data["action_id"]
    assert runtime.cancel_by_symbol_calls == 1
