from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, List, Sequence, Tuple

from src.persistence.schema import INSERT_PIPELINE_TRACE_EVENTS_SQL

if TYPE_CHECKING:
    from src.persistence.db import TimescaleWriter


class PipelineTraceRepository:
    def __init__(self, writer: "TimescaleWriter"):
        self._writer = writer

    def write_pipeline_trace_events(
        self,
        rows: Iterable[Tuple],
        page_size: int = 200,
    ) -> None:
        batch = []
        for row in rows:
            payload = row[6] if row[6] is not None else {}
            batch.append((*row[:6], self._writer._json(payload)))
        if not batch:
            return
        self._writer._batch(
            INSERT_PIPELINE_TRACE_EVENTS_SQL,
            batch,
            page_size=page_size,
        )

    def fetch_pipeline_trace_events(
        self,
        *,
        trace_ids: Sequence[str],
        limit: int = 500,
    ) -> List[dict[str, Any]]:
        normalized_trace_ids = [
            str(item).strip()
            for item in trace_ids
            if str(item or "").strip()
        ]
        if not normalized_trace_ids:
            return []
        sql = """
SELECT id, trace_id, symbol, timeframe, scope, event_type, recorded_at, payload
FROM pipeline_trace_events
WHERE trace_id = ANY(%s)
ORDER BY recorded_at ASC, id ASC
LIMIT %s
"""
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, [normalized_trace_ids, max(1, int(limit))])
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in rows]

    def fetch_pipeline_trace_filtered(
        self,
        *,
        trace_id: str | None = None,
        symbol: str | None = None,
        timeframe: str | None = None,
        event_type: str | None = None,
        scope: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """通用 pipeline trace 查询，支持多维过滤。"""
        conditions: list[str] = []
        params: list[Any] = []
        if trace_id:
            conditions.append("trace_id = %s")
            params.append(trace_id)
        if symbol:
            conditions.append("symbol = %s")
            params.append(symbol)
        if timeframe:
            conditions.append("timeframe = %s")
            params.append(timeframe)
        if event_type:
            conditions.append("event_type = %s")
            params.append(event_type)
        if scope:
            conditions.append("scope = %s")
            params.append(scope)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = (
            f"SELECT id, trace_id, symbol, timeframe, scope, event_type, "
            f"recorded_at, payload FROM pipeline_trace_events{where} "
            f"ORDER BY recorded_at DESC, id DESC LIMIT %s OFFSET %s"
        )
        params.extend([max(1, int(limit)), max(0, int(offset))])
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
