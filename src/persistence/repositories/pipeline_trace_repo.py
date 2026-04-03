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
