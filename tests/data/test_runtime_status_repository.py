from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.persistence.repositories.runtime_repo import RuntimeStatusRepository
from src.persistence.schema.runtime_tasks import UPSERT_SQL


class _Cursor:
    def __init__(self, rows=None) -> None:
        self.rows = rows or []
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params) -> None:
        self.executed.append((sql, params))

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self._cursor


def test_write_runtime_task_status_normalizes_missing_details() -> None:
    captured = {}

    class _Writer:
        def _json(self, payload):
            return {"wrapped": payload}

        def _batch(self, sql, batch, page_size=200):
            captured["sql"] = sql
            captured["batch"] = batch
            captured["page_size"] = page_size

    repo = RuntimeStatusRepository(_Writer())
    row = (
        "startup",
        "monitoring",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        "ready",
        None,
        None,
        None,
        123,
        1,
        0,
        0,
        None,
        None,
    )

    repo.write_runtime_task_status([row], page_size=50)

    assert captured["sql"] == UPSERT_SQL
    assert captured["page_size"] == 50
    assert captured["batch"] == [
        (
            "startup",
            "monitoring",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            "ready",
            None,
            None,
            None,
            123,
            1,
            0,
            0,
            None,
            {"wrapped": {}},
        )
    ]


def test_fetch_runtime_task_status_applies_filters_and_ordering() -> None:
    cursor = _Cursor(rows=[("startup", "monitoring", "ts", "ready", None, None, None, 123, 1, 0, 0, None, {})])

    class _Writer:
        def connection(self):
            return _Connection(cursor)

    repo = RuntimeStatusRepository(_Writer())

    rows = repo.fetch_runtime_task_status(component="startup", task_name="monitoring")

    assert rows == [("startup", "monitoring", "ts", "ready", None, None, None, 123, 1, 0, 0, None, {})]
    assert len(cursor.executed) == 1
    sql, params = cursor.executed[0]
    assert "FROM runtime_task_status WHERE 1=1" in sql
    assert "AND component = %s" in sql
    assert "AND task_name = %s" in sql
    assert "ORDER BY component ASC, task_name ASC" in sql
    assert params == ["startup", "monitoring"]
