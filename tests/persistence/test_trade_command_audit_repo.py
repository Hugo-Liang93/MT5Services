from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from src.persistence.repositories.trade_repo import TradeCommandAuditRepository
from src.trading.models import TradeCommandAuditRecord


class _DummyWriter:
    def __init__(self) -> None:
        self.batch_sql = None
        self.batch_rows = None
        self.executed_sql = None
        self.executed_params = None
        self.fetchone_result = (0,)

    def _json(self, payload):
        return {"wrapped": payload}

    def _batch(self, sql, rows, page_size: int = 200) -> None:
        self.batch_sql = sql
        self.batch_rows = list(rows)

    @contextmanager
    def connection(self):
        yield _Connection(self)


class _Connection:
    def __init__(self, writer: _DummyWriter) -> None:
        self._writer = writer

    def cursor(self):
        return _Cursor(self._writer)


class _Cursor:
    def __init__(self, writer: _DummyWriter) -> None:
        self._writer = writer

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params):
        self._writer.executed_sql = sql
        self._writer.executed_params = list(params)

    def fetchone(self):
        return self._writer.fetchone_result

    def fetchall(self):
        return []

    @property
    def description(self):
        return []


def test_write_trade_command_audits_maps_account_key_before_command_type() -> None:
    writer = _DummyWriter()
    repo = TradeCommandAuditRepository(writer)
    row = TradeCommandAuditRecord(
        recorded_at=datetime(2026, 4, 12, 15, 0, tzinfo=timezone.utc),
        operation_id="op_1",
        account_alias="live_main",
        account_key="live:broker-live:1001",
        command_type="update_trade_control",
        status="success",
        request_payload={"idempotency_key": "idem_1"},
        response_payload={"action_id": "act_1"},
    ).to_row()

    repo.write_trade_command_audits([row])

    assert writer.batch_rows is not None
    assert len(writer.batch_rows) == 1
    persisted = writer.batch_rows[0]
    assert persisted[0] == row[0]
    assert persisted[1] == "op_1"
    assert persisted[2] == "live_main"
    assert persisted[3] == "live:broker-live:1001"
    assert persisted[4] == "update_trade_control"
    assert persisted[16] == {"wrapped": {"idempotency_key": "idem_1"}}
    assert persisted[17] == {"wrapped": {"action_id": "act_1"}}


def test_count_successful_trade_commands_since_is_account_scoped() -> None:
    writer = _DummyWriter()
    writer.fetchone_result = (7,)
    repo = TradeCommandAuditRepository(writer)
    since = datetime(2026, 5, 5, 0, 0, tzinfo=timezone.utc)

    count = repo.count_successful_trade_commands_since(
        since=since,
        account_key="live:broker:123",
    )

    assert count == 7
    assert "command_type = %s" in writer.executed_sql
    assert "status = %s" in writer.executed_sql
    assert "account_key = %s" in writer.executed_sql
    assert writer.executed_params == [
        since,
        "execute_trade",
        "success",
        "live:broker:123",
    ]


def test_count_successful_trade_commands_since_excludes_dry_run_audits() -> None:
    writer = _DummyWriter()
    repo = TradeCommandAuditRepository(writer)
    since = datetime(2026, 5, 5, 0, 0, tzinfo=timezone.utc)

    repo.count_successful_trade_commands_since(
        since=since,
        account_key="live:broker:123",
    )

    assert "response_payload->>'dry_run'" in writer.executed_sql
    assert "request_payload->>'dry_run'" in writer.executed_sql
    assert "NOT IN ('true', '1', 'yes', 'on')" in writer.executed_sql


def test_trace_operation_lookup_matches_request_context_trace_id() -> None:
    writer = _DummyWriter()
    repo = TradeCommandAuditRepository(writer)

    repo.fetch_trace_operations_by_trace_id(
        account_alias="demo_main",
        trace_id="sig-1",
    )

    assert "request_payload #>> '{request_context,trace_id}'" in writer.executed_sql
    assert "response_payload #>> '{request_context,trace_id}'" in writer.executed_sql


def test_signal_trace_operation_lookup_matches_request_context_trace_id() -> None:
    writer = _DummyWriter()
    repo = TradeCommandAuditRepository(writer)

    repo.fetch_trace_operations(
        account_alias="demo_main",
        signal_id="sig-1",
    )

    assert "request_payload #>> '{request_context,trace_id}'" in writer.executed_sql
    assert "response_payload #>> '{request_context,trace_id}'" in writer.executed_sql
