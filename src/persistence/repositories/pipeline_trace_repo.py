from __future__ import annotations

from datetime import datetime
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
            batch.append(
                (
                    *row[:6],
                    self._writer._json(payload),
                    row[7],
                    row[8],
                    row[9],
                    row[10],
                    row[11],
                    row[12],
                    row[13],
                )
            )
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
SELECT id, trace_id, symbol, timeframe, scope, event_type, recorded_at, payload,
       instance_id, instance_role, account_key, signal_id, intent_id, command_id, action_id
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

    def query_trace_summaries(
        self,
        *,
        trace_id: str | None = None,
        symbol: str | None = None,
        timeframe: str | None = None,
        strategy: str | None = None,
        signal_id: str | None = None,
        status: str | None = None,
        from_time: Any = None,
        to_time: Any = None,
        page: int = 1,
        page_size: int = 100,
        sort: str = "last_event_at_desc",
    ) -> dict[str, Any]:
        sort_token = str(sort or "last_event_at_desc").strip().lower()
        if sort_token in {"last_event_at_asc", "asc"}:
            order_by = "last_event_at ASC, trace_id ASC"
        elif sort_token == "started_at_asc":
            order_by = "started_at ASC, trace_id ASC"
        elif sort_token == "started_at_desc":
            order_by = "started_at DESC, trace_id DESC"
        else:
            order_by = "last_event_at DESC, trace_id DESC"

        effective_page = max(1, int(page))
        effective_page_size = max(1, int(page_size))
        offset = (effective_page - 1) * effective_page_size

        sql = """
WITH filtered AS (
    SELECT id,
           trace_id,
           symbol,
           timeframe,
           event_type,
           recorded_at,
           payload,
           signal_id,
           intent_id,
           command_id,
           action_id
    FROM pipeline_trace_events
    WHERE 1=1
"""
        params: list[Any] = []
        if trace_id:
            sql += " AND trace_id = %s"
            params.append(trace_id)
        if symbol:
            sql += " AND symbol = %s"
            params.append(symbol)
        if timeframe:
            sql += " AND timeframe = %s"
            params.append(timeframe)
        if from_time is not None:
            sql += " AND recorded_at >= %s"
            params.append(from_time)
        if to_time is not None:
            sql += " AND recorded_at <= %s"
            params.append(to_time)
        sql += """
),
ranked AS (
    SELECT id,
           trace_id,
           symbol,
           timeframe,
           event_type,
           recorded_at,
           payload,
           signal_id,
           intent_id,
           command_id,
           action_id,
           ROW_NUMBER() OVER (PARTITION BY trace_id ORDER BY recorded_at DESC, id DESC) AS rn_desc
    FROM filtered
),
summary AS (
    SELECT trace_id,
           MAX(symbol) AS symbol,
           MAX(timeframe) AS timeframe,
           MIN(recorded_at) AS started_at,
           MAX(recorded_at) AS last_event_at,
           COUNT(*)::bigint AS event_count,
           MAX(CASE WHEN rn_desc = 1 THEN event_type END) AS last_event_type,
           MAX(CASE WHEN rn_desc = 1 THEN COALESCE(payload->>'reason', payload->>'skip_reason', payload->>'category', '') END) AS reason,
           MAX(CASE WHEN rn_desc = 1 THEN COALESCE(payload->>'signal_state', '') END) AS last_signal_state,
           MAX(CASE WHEN rn_desc = 1 THEN COALESCE(payload->>'direction', '') END) AS last_direction,
           MAX(CASE WHEN rn_desc = 1 THEN COALESCE(payload->>'winning_direction', '') END) AS last_winning_direction,
           MAX(NULLIF(payload->>'strategy', '')) AS strategy,
           MAX(COALESCE(NULLIF(signal_id, ''), NULLIF(payload->>'signal_id', ''), NULLIF(payload->>'request_id', ''))) AS signal_id,
           MAX(NULLIF(intent_id, '')) AS intent_id,
           MAX(NULLIF(command_id, '')) AS command_id,
           MAX(NULLIF(action_id, '')) AS action_id,
           MAX(CASE WHEN rn_desc = 1 THEN COALESCE(payload->>'allowed', '') END) AS last_allowed,
           MAX(
               CASE
                   WHEN event_type = 'admission_report_appended'
                   THEN COALESCE(payload->>'decision', '')
               END
           ) AS last_admission_decision,
           MAX(
               CASE
                   WHEN event_type = 'admission_report_appended'
                   THEN COALESCE(payload->>'stage', '')
               END
           ) AS last_admission_stage
    FROM ranked
    GROUP BY trace_id
)
SELECT trace_id,
       symbol,
       timeframe,
       strategy,
       signal_id,
       intent_id,
       command_id,
       action_id,
       started_at,
       last_event_at,
       event_count,
       last_event_type,
       reason,
       NULLIF(last_signal_state, '') AS last_signal_state,
       NULLIF(last_direction, '') AS last_direction,
       NULLIF(last_winning_direction, '') AS last_winning_direction,
       NULLIF(last_admission_decision, '') AS last_admission_decision,
       NULLIF(last_admission_stage, '') AS last_admission_stage,
       CASE
            WHEN last_event_type IN ('command_failed', 'execution_failed') THEN 'failed'
            WHEN last_event_type IN ('execution_blocked', 'signal_filter_blocked') THEN 'blocked'
            WHEN last_event_type IN ('intent_dead_lettered') THEN 'dead_lettered'
            WHEN last_event_type IN ('execution_skipped') THEN 'skipped'
           WHEN last_event_type IN ('command_completed', 'close_completed', 'execution_succeeded', 'execution_submitted', 'pending_order_submitted') THEN 'submitted'
           WHEN last_event_type IN ('intent_claimed', 'command_claimed') THEN 'claimed'
           WHEN last_event_type = 'admission_report_appended' THEN 'admission'
           WHEN last_event_type = 'execution_decided' THEN 'execution_ready'
           WHEN last_event_type = 'voting_completed'
                AND NULLIF(last_winning_direction, '') IS NULL THEN 'no_signal'
           WHEN last_event_type = 'signal_evaluated'
                AND (
                    COALESCE(last_signal_state, '') = 'no_signal'
                    OR COALESCE(last_direction, '') NOT IN ('buy', 'sell')
                ) THEN 'no_signal'
           WHEN last_event_type = 'signal_evaluated' THEN 'signal_evaluated'
           WHEN last_event_type = 'signal_filter_decided' AND last_allowed = 'false' THEN 'blocked'
           WHEN last_event_type = 'signal_filter_decided' AND last_allowed = 'true' THEN 'filtered_pass'
           ELSE 'in_progress'
       END AS status,
       COUNT(*) OVER() AS total_count
FROM summary
WHERE 1=1
"""
        if strategy:
            sql += " AND strategy = %s"
            params.append(strategy)
        if signal_id:
            sql += " AND signal_id = %s"
            params.append(signal_id)
        if status:
            sql += """
 AND CASE
         WHEN last_event_type IN ('command_failed', 'execution_failed') THEN 'failed'
         WHEN last_event_type IN ('execution_blocked', 'signal_filter_blocked') THEN 'blocked'
         WHEN last_event_type IN ('intent_dead_lettered') THEN 'dead_lettered'
         WHEN last_event_type IN ('execution_skipped') THEN 'skipped'
         WHEN last_event_type IN ('command_completed', 'close_completed', 'execution_succeeded', 'execution_submitted', 'pending_order_submitted') THEN 'submitted'
         WHEN last_event_type IN ('intent_claimed', 'command_claimed') THEN 'claimed'
         WHEN last_event_type = 'admission_report_appended' THEN 'admission'
         WHEN last_event_type = 'execution_decided' THEN 'execution_ready'
         WHEN last_event_type = 'voting_completed'
              AND NULLIF(last_winning_direction, '') IS NULL THEN 'no_signal'
         WHEN last_event_type = 'signal_evaluated'
              AND (
                  COALESCE(last_signal_state, '') = 'no_signal'
                  OR COALESCE(last_direction, '') NOT IN ('buy', 'sell')
              ) THEN 'no_signal'
         WHEN last_event_type = 'signal_evaluated' THEN 'signal_evaluated'
         WHEN last_event_type = 'signal_filter_decided' AND last_allowed = 'false' THEN 'blocked'
         WHEN last_event_type = 'signal_filter_decided' AND last_allowed = 'true' THEN 'filtered_pass'
         ELSE 'in_progress'
     END = %s
"""
            params.append(status)
        sql += f" ORDER BY {order_by} LIMIT %s OFFSET %s"
        params.extend([effective_page_size, offset])

        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
        items = [dict(zip(columns, row)) for row in rows]
        total = int(items[0].get("total_count") or 0) if items else 0
        return {
            "items": items,
            "total": total,
            "page": effective_page,
            "page_size": effective_page_size,
        }

    def fetch_pipeline_trace_filtered(
        self,
        *,
        trace_id: str | None = None,
        instance_id: str | None = None,
        account_key: str | None = None,
        symbol: str | None = None,
        timeframe: str | None = None,
        event_type: str | None = None,
        event_types: Sequence[str] | None = None,
        scope: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if trace_id:
            conditions.append("trace_id = %s")
            params.append(trace_id)
        if instance_id:
            conditions.append("instance_id = %s")
            params.append(instance_id)
        if account_key:
            conditions.append("account_key = %s")
            params.append(account_key)
        if symbol:
            conditions.append("symbol = %s")
            params.append(symbol)
        if timeframe:
            conditions.append("timeframe = %s")
            params.append(timeframe)
        if event_type:
            conditions.append("event_type = %s")
            params.append(event_type)
        normalized_event_types = [
            str(item).strip()
            for item in (event_types or [])
            if str(item or "").strip()
        ]
        if normalized_event_types:
            conditions.append("event_type = ANY(%s)")
            params.append(normalized_event_types)
        if scope:
            conditions.append("scope = %s")
            params.append(scope)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = (
            "SELECT id, trace_id, symbol, timeframe, scope, event_type, "
            "recorded_at, payload, instance_id, instance_role, account_key, "
            "signal_id, intent_id, command_id, action_id "
            f"FROM pipeline_trace_events{where} "
            "ORDER BY recorded_at DESC, id DESC LIMIT %s OFFSET %s"
        )
        params.extend([max(1, int(limit)), max(0, int(offset))])
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in rows]

    def fetch_gate_events(
        self,
        *,
        from_time: datetime,
        to_time: datetime,
        symbol: str | None = None,
        timeframes: Sequence[str] | None = None,
        limit: int = 50000,
    ) -> list[dict[str, Any]]:
        normalized_timeframes = [
            str(item).strip()
            for item in (timeframes or [])
            if str(item or "").strip()
        ]
        sql = """
SELECT trace_id,
       symbol,
       timeframe,
       scope,
       event_type,
       recorded_at,
       COALESCE(
           NULLIF(payload->>'reason', ''),
           NULLIF(payload->>'skip_reason', ''),
           NULLIF(payload->>'category', ''),
           NULLIF(payload->>'skip_category', '')
       ) AS gate_reason,
       COALESCE(
           NULLIF(payload->>'category', ''),
           NULLIF(payload->>'skip_category', '')
       ) AS gate_category,
       CASE
           WHEN event_type = 'signal_filter_decided' THEN 'signal_filter'
           WHEN event_type IN ('execution_blocked', 'execution_skipped') THEN 'execution'
           ELSE event_type
       END AS gate_source,
       COALESCE(
           CASE
               WHEN NULLIF(payload->>'evaluation_time', '') IS NOT NULL
               THEN (payload->>'evaluation_time')::timestamptz
           END,
           CASE
               WHEN NULLIF(payload->>'bar_time', '') IS NOT NULL
               THEN (payload->>'bar_time')::timestamptz
           END,
           recorded_at
       ) AS evaluation_time
FROM pipeline_trace_events
WHERE recorded_at >= %s
  AND recorded_at <= %s
  AND (
        (event_type = 'signal_filter_decided' AND COALESCE(payload->>'allowed', '') = 'false')
        OR event_type IN ('execution_blocked', 'execution_skipped')
      )
  AND COALESCE(
        NULLIF(payload->>'reason', ''),
        NULLIF(payload->>'skip_reason', ''),
        NULLIF(payload->>'category', ''),
        NULLIF(payload->>'skip_category', '')
      ) IS NOT NULL
"""
        params: list[Any] = [from_time, to_time]
        if symbol:
            sql += " AND symbol = %s"
            params.append(symbol)
        if normalized_timeframes:
            sql += " AND timeframe = ANY(%s)"
            params.append(normalized_timeframes)
        sql += """
ORDER BY recorded_at DESC, trace_id DESC
LIMIT %s
"""
        params.append(max(1, int(limit)))
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in rows]
