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
            batch.append(
                (
                    *row[:12],
                    self._writer._json(details),
                    row[13],
                    row[14],
                    row[15],
                    row[16],
                )
            )
        if not batch:
            return
        self._writer._batch(UPSERT_RUNTIME_TASK_STATUS_SQL, batch, page_size=page_size)

    def fetch_runtime_task_status(
        self,
        component: Optional[str] = None,
        task_name: Optional[str] = None,
        instance_id: Optional[str] = None,
        instance_role: Optional[str] = None,
        account_key: Optional[str] = None,
        account_alias: Optional[str] = None,
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
        if instance_id is not None:
            sql += " AND instance_id = %s"
            params.append(instance_id)
        if instance_role is not None:
            sql += " AND instance_role = %s"
            params.append(instance_role)
        if account_key is not None:
            sql += " AND account_key = %s"
            params.append(account_key)
        if account_alias is not None:
            sql += " AND account_alias = %s"
            params.append(account_alias)
        sql += " ORDER BY updated_at DESC, component ASC, task_name ASC"
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def fetch_runtime_task_status_records(
        self,
        component: Optional[str] = None,
        task_name: Optional[str] = None,
        instance_id: Optional[str] = None,
        instance_role: Optional[str] = None,
        account_key: Optional[str] = None,
        account_alias: Optional[str] = None,
    ) -> List[dict]:
        sql = (
            "SELECT component, task_name, updated_at, state, started_at, completed_at, "
            "next_run_at, duration_ms, success_count, failure_count, consecutive_failures, "
            "last_error, details, instance_id, instance_role, account_key, account_alias "
            "FROM runtime_task_status WHERE 1=1"
        )
        params: List = []
        if component is not None:
            sql += " AND component = %s"
            params.append(component)
        if task_name is not None:
            sql += " AND task_name = %s"
            params.append(task_name)
        if instance_id is not None:
            sql += " AND instance_id = %s"
            params.append(instance_id)
        if instance_role is not None:
            sql += " AND instance_role = %s"
            params.append(instance_role)
        if account_key is not None:
            sql += " AND account_key = %s"
            params.append(account_key)
        if account_alias is not None:
            sql += " AND account_alias = %s"
            params.append(account_alias)
        sql += " ORDER BY updated_at DESC, component ASC, task_name ASC"
        columns = (
            "component",
            "task_name",
            "updated_at",
            "state",
            "started_at",
            "completed_at",
            "next_run_at",
            "duration_ms",
            "success_count",
            "failure_count",
            "consecutive_failures",
            "last_error",
            "details",
            "instance_id",
            "instance_role",
            "account_key",
            "account_alias",
        )
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(zip(columns, row)) for row in cur.fetchall()]
