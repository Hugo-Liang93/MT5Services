from __future__ import annotations

from src.persistence.repositories.pipeline_trace_repo import PipelineTraceRepository
from src.persistence.schema.pipeline_trace_events import DDL as PIPELINE_TRACE_EVENTS_DDL


class _Cursor:
    def __init__(self, writer: "_Writer") -> None:
        self._writer = writer
        self.description = [
            ("id",),
            ("trace_id",),
            ("symbol",),
            ("timeframe",),
            ("scope",),
            ("event_type",),
            ("recorded_at",),
            ("payload",),
            ("instance_id",),
            ("instance_role",),
            ("account_key",),
            ("signal_id",),
            ("intent_id",),
            ("command_id",),
            ("action_id",),
        ]

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *args) -> None:
        return None

    def execute(self, sql: str, params: list[object]) -> None:
        self._writer.last_sql = sql
        self._writer.last_params = list(params)

    def fetchall(self) -> list[tuple]:
        return []


class _Connection:
    def __init__(self, writer: "_Writer") -> None:
        self._writer = writer

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *args) -> None:
        return None

    def cursor(self) -> _Cursor:
        return _Cursor(self._writer)


class _Writer:
    def __init__(self) -> None:
        self.last_sql: str | None = None
        self.last_params: list[object] | None = None

    def connection(self) -> _Connection:
        return _Connection(self)


def test_pipeline_trace_filtered_accepts_execution_identifiers() -> None:
    writer = _Writer()
    repo = PipelineTraceRepository(writer)

    rows = repo.fetch_pipeline_trace_filtered(
        intent_id="intent_1",
        command_id="cmd_1",
        action_id="act_1",
        limit=25,
        offset=5,
    )

    assert rows == []
    assert writer.last_sql is not None
    assert "intent_id = %s" in writer.last_sql
    assert "command_id = %s" in writer.last_sql
    assert "action_id = %s" in writer.last_sql
    assert writer.last_params == ["intent_1", "cmd_1", "act_1", 25, 5]


def test_query_trace_summaries_accepts_execution_identifier_filters() -> None:
    writer = _Writer()
    repo = PipelineTraceRepository(writer)

    result = repo.query_trace_summaries(
        intent_id="intent_1",
        command_id="cmd_1",
        action_id="act_1",
        page_size=20,
    )

    assert result["items"] == []
    assert writer.last_sql is not None
    assert "intent_id = %s" in writer.last_sql
    assert "command_id = %s" in writer.last_sql
    assert "action_id = %s" in writer.last_sql
    assert writer.last_params == ["intent_1", "cmd_1", "act_1", 20, 0]


def test_pipeline_trace_scope_contract_covers_signal_tick_and_operator_domains() -> None:
    ddl = " ".join(PIPELINE_TRACE_EVENTS_DDL.split())

    assert "DROP CONSTRAINT IF EXISTS pipeline_trace_events_scope_check" in ddl
    assert "ADD CONSTRAINT pipeline_trace_events_scope_check" in ddl
    for scope in (
        "confirmed",
        "intrabar",
        "tick_derived",
        "trade_control",
        "position",
        "positions",
        "exposure",
        "orders",
        "runtime_mode",
    ):
        assert f"'{scope}'" in ddl
