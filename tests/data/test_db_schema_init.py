from __future__ import annotations

from src.persistence.db import TimescaleWriter
from src.persistence.schema import POST_INIT_DDL_STATEMENTS


class _Cursor:
    def __init__(self, executed: list[str]) -> None:
        self._executed = executed

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str) -> None:
        self._executed.append(sql)


class _Connection:
    def __init__(self, executed: list[str]) -> None:
        self._executed = executed

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _Cursor(self._executed)


def test_init_schema_runs_post_init_migrations() -> None:
    executed: list[str] = []
    writer = TimescaleWriter.__new__(TimescaleWriter)
    writer.connection = lambda: _Connection(executed)  # type: ignore[assignment]

    writer.init_schema()

    assert len(executed) == 1 + len(POST_INIT_DDL_STATEMENTS)
    assert "CREATE EXTENSION IF NOT EXISTS timescaledb" in executed[0]
    assert any("runtime_task_status" in sql for sql in executed[1:])
    assert any("economic_calendar_events" in sql for sql in executed[1:])
