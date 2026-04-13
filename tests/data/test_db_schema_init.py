from __future__ import annotations

import re

from src.persistence.db import TimescaleWriter
from src.persistence.schema import DDL_STATEMENTS, POST_INIT_DDL_STATEMENTS


class _Cursor:
    def __init__(self, executed: list[str]) -> None:
        self._executed = executed

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params=None) -> None:
        if params is None:
            self._executed.append(sql)
            return
        self._executed.append(f"{sql} | params={params!r}")


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

    assert len(executed) == 3 + len(POST_INIT_DDL_STATEMENTS)
    assert "SELECT pg_advisory_lock" in executed[0]
    assert "CREATE EXTENSION IF NOT EXISTS timescaledb" in executed[1]
    assert any("runtime_task_status" in sql for sql in executed[2:-1])
    assert any("economic_calendar_events" in sql for sql in executed[2:-1])
    assert "SELECT pg_advisory_unlock" in executed[-1]


def test_hypertable_unique_indexes_include_partition_column() -> None:
    hypertable_pattern = re.compile(
        r"create_hypertable\('(?P<table>[^']+)',\s*'(?P<partition>[^']+)'",
        re.IGNORECASE | re.DOTALL,
    )
    unique_index_pattern = re.compile(
        r"CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+\S+\s+ON\s+(?P<table>\w+)\s*\((?P<columns>[^)]+)\)",
        re.IGNORECASE,
    )

    for ddl in DDL_STATEMENTS:
        hypertable_match = hypertable_pattern.search(ddl)
        if hypertable_match is None:
            continue
        table = hypertable_match.group("table").lower()
        partition_column = hypertable_match.group("partition").lower()

        for index_match in unique_index_pattern.finditer(ddl):
            if index_match.group("table").lower() != table:
                continue
            normalized_columns = {
                column.strip().split()[0].strip('"').lower()
                for column in index_match.group("columns").split(",")
            }
            assert partition_column in normalized_columns, (
                f"hypertable {table} unique index must include partition column "
                f"{partition_column}: {index_match.group(0)}"
            )
