from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, List, Optional, Tuple

from src.persistence.schema import INSERT_TRADE_COMMAND_AUDITS_SQL

if TYPE_CHECKING:
    from src.persistence.db import TimescaleWriter


class TradeCommandAuditRepository:
    def __init__(self, writer: "TimescaleWriter"):
        self._writer = writer

    def write_trade_command_audits(self, rows: Iterable[Tuple], page_size: int = 200) -> None:
        batch = []
        for row in rows:
            request_payload = row[15] if row[15] is not None else {}
            response_payload = row[16] if row[16] is not None else {}
            batch.append((*row[:15], self._writer._json(request_payload), self._writer._json(response_payload)))
        if not batch:
            return
        self._writer._batch(INSERT_TRADE_COMMAND_AUDITS_SQL, batch, page_size=page_size)

    def fetch_trade_command_audits(
        self,
        *,
        account_alias: Optional[str] = None,
        command_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Tuple]:
        sql = (
            "SELECT recorded_at, operation_id, account_alias, command_type, status, "
            "symbol, side, order_kind, volume, ticket, order_id, deal_id, magic, "
            "duration_ms, error_message, request_payload, response_payload "
            "FROM trade_command_audits WHERE 1=1"
        )
        params: List = []
        if account_alias is not None:
            sql += " AND account_alias = %s"
            params.append(account_alias)
        if command_type is not None:
            sql += " AND command_type = %s"
            params.append(command_type)
        if status is not None:
            sql += " AND status = %s"
            params.append(status)
        sql += " ORDER BY recorded_at DESC LIMIT %s"
        params.append(limit)
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def summarize_trade_command_audits(
        self,
        *,
        hours: int = 24,
        account_alias: Optional[str] = None,
    ) -> List[Tuple]:
        sql = (
            "SELECT account_alias, command_type, status, COUNT(*) AS count, "
            "AVG(duration_ms)::double precision AS avg_duration_ms, MAX(recorded_at) AS last_seen_at "
            "FROM trade_command_audits "
            "WHERE recorded_at >= NOW() - (%s * INTERVAL '1 hour')"
        )
        params: List = [max(1, int(hours))]
        if account_alias is not None:
            sql += " AND account_alias = %s"
            params.append(account_alias)
        sql += " GROUP BY account_alias, command_type, status ORDER BY account_alias, command_type, status"
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def fetch_trace_operations(
        self,
        *,
        account_alias: str,
        signal_id: str,
        limit: int = 100,
    ) -> List[dict[str, Any]]:
        sql = """
SELECT recorded_at, operation_id, account_alias, command_type, status,
       symbol, side, order_kind, volume, ticket, order_id, deal_id, magic,
       duration_ms, error_message, request_payload, response_payload
FROM trade_command_audits
WHERE account_alias = %s
  AND (
        COALESCE(request_payload->>'request_id', '') = %s
        OR COALESCE(response_payload->>'request_id', '') = %s
        OR COALESCE(request_payload #>> '{metadata,signal,signal_id}', '') = %s
        OR COALESCE(response_payload #>> '{metadata,signal,signal_id}', '') = %s
      )
ORDER BY recorded_at ASC
LIMIT %s
"""
        return self._fetch_dicts(
            sql,
            [
                account_alias,
                signal_id,
                signal_id,
                signal_id,
                signal_id,
                max(1, int(limit)),
            ],
        )

    def _fetch_dicts(self, sql: str, params: List[Any]) -> List[dict[str, Any]]:
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in rows]
