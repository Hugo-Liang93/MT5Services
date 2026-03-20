from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, List, Optional, Tuple

from src.persistence.schema import UPSERT_RUNTIME_TASK_STATUS_SQL

if TYPE_CHECKING:
    from src.persistence.db import TimescaleWriter


class RuntimeStatusRepository:
    def __init__(self, writer: "TimescaleWriter"):
        self._writer = writer

    def write_runtime_task_status(self, rows: Iterable[Tuple], page_size: int = 200) -> None:
        batch = []
        for row in rows:
            details = row[12] if row[12] is not None else {}
            batch.append((*row[:12], self._writer._json(details)))
        if not batch:
            return
        self._writer._batch(UPSERT_RUNTIME_TASK_STATUS_SQL, batch, page_size=page_size)

    def fetch_runtime_task_status(
        self,
        component: Optional[str] = None,
        task_name: Optional[str] = None,
    ) -> List[Tuple]:
        sql = (
            "SELECT component, task_name, updated_at, state, started_at, completed_at, "
            "next_run_at, duration_ms, success_count, failure_count, consecutive_failures, "
            "last_error, details "
            "FROM runtime_task_status WHERE 1=1"
        )
        params: List = []
        if component is not None:
            sql += " AND component = %s"
            params.append(component)
        if task_name is not None:
            sql += " AND task_name = %s"
            params.append(task_name)
        sql += " ORDER BY component ASC, task_name ASC"
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
