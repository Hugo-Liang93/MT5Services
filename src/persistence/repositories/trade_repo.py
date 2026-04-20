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
            batch.append(
                (
                    row[0],
                    row[1],
                    row[2],
                    row[17],
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                    row[8],
                    row[9],
                    row[10],
                    row[11],
                    row[12],
                    row[13],
                    row[14],
                    self._writer._json(request_payload),
                    self._writer._json(response_payload),
                )
            )
        if not batch:
            return
        self._writer._batch(INSERT_TRADE_COMMAND_AUDITS_SQL, batch, page_size=page_size)

    def fetch_trade_command_audits(
        self,
        *,
        account_alias: Optional[str] = None,
        account_key: Optional[str] = None,
        command_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Tuple]:
        sql = (
            "SELECT recorded_at, operation_id, account_alias, command_type, status, "
            "symbol, side, order_kind, volume, ticket, order_id, deal_id, magic, "
            "duration_ms, error_message, request_payload, response_payload, account_key "
            "FROM trade_command_audits WHERE 1=1"
        )
        params: List = []
        if account_key is not None:
            sql += " AND account_key = %s"
            params.append(account_key)
        elif account_alias is not None:
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
        account_key: Optional[str] = None,
    ) -> List[Tuple]:
        sql = (
            "SELECT account_alias, command_type, status, COUNT(*) AS count, "
            "AVG(duration_ms)::double precision AS avg_duration_ms, MAX(recorded_at) AS last_seen_at "
            "FROM trade_command_audits "
            "WHERE recorded_at >= NOW() - (%s * INTERVAL '1 hour')"
        )
        params: List = [max(1, int(hours))]
        if account_key is not None:
            sql += " AND account_key = %s"
            params.append(account_key)
        elif account_alias is not None:
            sql += " AND account_alias = %s"
            params.append(account_alias)
        sql += " GROUP BY account_alias, command_type, status ORDER BY account_alias, command_type, status"
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def query_trade_command_audits(
        self,
        *,
        account_alias: Optional[str] = None,
        account_key: Optional[str] = None,
        command_type: Optional[str] = None,
        status: Optional[str] = None,
        symbol: Optional[str] = None,
        signal_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        actor: Optional[str] = None,
        audit_id: Optional[str] = None,
        action_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        from_time: Optional[Any] = None,
        to_time: Optional[Any] = None,
        page: int = 1,
        page_size: int = 100,
        sort: str = "recorded_at_desc",
    ) -> dict[str, Any]:
        sort_token = str(sort or "recorded_at_desc").strip().lower()
        sort_direction = "ASC" if sort_token in {"recorded_at_asc", "asc"} else "DESC"
        effective_page = max(1, int(page))
        effective_page_size = max(1, int(page_size))
        offset = (effective_page - 1) * effective_page_size

        trace_expr = (
            "COALESCE(NULLIF(request_payload->>'trace_id', ''), "
            "NULLIF(response_payload->>'trace_id', ''))"
        )
        signal_expr = (
            "COALESCE(NULLIF(request_payload #>> '{metadata,signal,signal_id}', ''), "
            "NULLIF(response_payload #>> '{metadata,signal,signal_id}', ''), "
            "NULLIF(request_payload->>'request_id', ''), "
            "NULLIF(response_payload->>'request_id', ''))"
        )
        actor_expr = (
            "COALESCE(NULLIF(request_payload->>'actor', ''), "
            "NULLIF(response_payload->>'actor', ''), "
            "NULLIF(request_payload #>> '{metadata,actor}', ''), "
            "NULLIF(response_payload #>> '{metadata,actor}', ''))"
        )
        request_id_expr = (
            "COALESCE(NULLIF(request_payload->>'request_id', ''), "
            "NULLIF(response_payload->>'request_id', ''))"
        )
        action_expr = (
            "COALESCE(NULLIF(request_payload->>'action_id', ''), "
            "NULLIF(response_payload->>'action_id', ''))"
        )
        idempotency_expr = (
            "COALESCE(NULLIF(request_payload->>'idempotency_key', ''), "
            "NULLIF(response_payload->>'idempotency_key', ''))"
        )
        reason_expr = (
            "COALESCE(NULLIF(request_payload->>'reason', ''), "
            "NULLIF(response_payload->>'reason', ''))"
        )

        sql = f"""
SELECT recorded_at,
       operation_id,
       account_alias,
       account_key,
       command_type,
       status,
       symbol,
       side,
       order_kind,
       volume,
       ticket,
       order_id,
       deal_id,
       magic,
       duration_ms,
       error_message,
       request_payload,
       response_payload,
       {trace_expr} AS trace_id,
       {signal_expr} AS signal_id,
       {actor_expr} AS actor,
       {request_id_expr} AS request_id,
       {action_expr} AS action_id,
       {idempotency_expr} AS idempotency_key,
       {reason_expr} AS reason,
       COUNT(*) OVER() AS total_count
FROM trade_command_audits
WHERE 1=1
"""
        params: list[Any] = []
        if account_key is not None:
            sql += " AND account_key = %s"
            params.append(account_key)
        elif account_alias is not None:
            sql += " AND account_alias = %s"
            params.append(account_alias)
        if command_type is not None:
            sql += " AND command_type = %s"
            params.append(command_type)
        if status is not None:
            sql += " AND status = %s"
            params.append(status)
        if symbol is not None:
            sql += " AND symbol = %s"
            params.append(symbol)
        if signal_id is not None:
            sql += f" AND {signal_expr} = %s"
            params.append(signal_id)
        if trace_id is not None:
            sql += f" AND {trace_expr} = %s"
            params.append(trace_id)
        if actor is not None:
            sql += f" AND {actor_expr} = %s"
            params.append(actor)
        if audit_id is not None:
            sql += " AND operation_id = %s"
            params.append(audit_id)
        if action_id is not None:
            sql += f" AND {action_expr} = %s"
            params.append(action_id)
        if idempotency_key is not None:
            sql += f" AND {idempotency_expr} = %s"
            params.append(idempotency_key)
        if from_time is not None:
            sql += " AND recorded_at >= %s"
            params.append(from_time)
        if to_time is not None:
            sql += " AND recorded_at <= %s"
            params.append(to_time)
        sql += (
            f" ORDER BY recorded_at {sort_direction}, operation_id {sort_direction} "
            "LIMIT %s OFFSET %s"
        )
        params.extend([effective_page_size, offset])

        rows = self._fetch_dicts(sql, params)
        total = int(rows[0].get("total_count") or 0) if rows else 0
        items = [self._normalize_audit_row(row) for row in rows]
        return {
            "items": items,
            "total": total,
            "page": effective_page,
            "page_size": effective_page_size,
        }

    def fetch_trade_command_audit_by_id(
        self,
        *,
        audit_id: str,
        account_alias: Optional[str] = None,
        account_key: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """按 audit_id（operation_id）单条查询 + 派生 action_id/idempotency_key/actor/reason。"""
        trace_expr = (
            "COALESCE(NULLIF(request_payload->>'trace_id', ''), "
            "NULLIF(response_payload->>'trace_id', ''))"
        )
        signal_expr = (
            "COALESCE(NULLIF(request_payload #>> '{metadata,signal,signal_id}', ''), "
            "NULLIF(response_payload #>> '{metadata,signal,signal_id}', ''), "
            "NULLIF(request_payload->>'request_id', ''), "
            "NULLIF(response_payload->>'request_id', ''))"
        )
        actor_expr = (
            "COALESCE(NULLIF(request_payload->>'actor', ''), "
            "NULLIF(response_payload->>'actor', ''), "
            "NULLIF(request_payload #>> '{metadata,actor}', ''), "
            "NULLIF(response_payload #>> '{metadata,actor}', ''))"
        )
        request_id_expr = (
            "COALESCE(NULLIF(request_payload->>'request_id', ''), "
            "NULLIF(response_payload->>'request_id', ''))"
        )
        action_expr = (
            "COALESCE(NULLIF(request_payload->>'action_id', ''), "
            "NULLIF(response_payload->>'action_id', ''))"
        )
        idempotency_expr = (
            "COALESCE(NULLIF(request_payload->>'idempotency_key', ''), "
            "NULLIF(response_payload->>'idempotency_key', ''))"
        )
        reason_expr = (
            "COALESCE(NULLIF(request_payload->>'reason', ''), "
            "NULLIF(response_payload->>'reason', ''))"
        )
        sql = f"""
SELECT recorded_at,
       operation_id,
       account_alias,
       account_key,
       command_type,
       status,
       symbol,
       side,
       order_kind,
       volume,
       ticket,
       order_id,
       deal_id,
       magic,
       duration_ms,
       error_message,
       request_payload,
       response_payload,
       {trace_expr} AS trace_id,
       {signal_expr} AS signal_id,
       {actor_expr} AS actor,
       {request_id_expr} AS request_id,
       {action_expr} AS action_id,
       {idempotency_expr} AS idempotency_key,
       {reason_expr} AS reason
FROM trade_command_audits
WHERE operation_id = %s
"""
        params: list[Any] = [audit_id]
        if account_key is not None:
            sql += " AND account_key = %s"
            params.append(account_key)
        elif account_alias is not None:
            sql += " AND account_alias = %s"
            params.append(account_alias)
        sql += " ORDER BY recorded_at DESC LIMIT 1"
        rows = self._fetch_dicts(sql, params)
        if not rows:
            return None
        return self._normalize_audit_row(rows[0])

    def fetch_linked_operator_command(
        self,
        *,
        audit_id: str,
    ) -> Optional[dict[str, Any]]:
        """通过 audit_id 反查 operator_commands 记录（回执链路闭环关键）。"""
        sql = """
SELECT created_at,
       command_id,
       command_type,
       target_account_key,
       target_account_alias,
       status,
       action_id,
       actor,
       reason,
       idempotency_key,
       attempt_count,
       last_error_code,
       completed_at,
       audit_id
FROM operator_commands
WHERE audit_id = %s
ORDER BY created_at DESC
LIMIT 1
"""
        rows = self._fetch_dicts(sql, [audit_id])
        if not rows:
            return None
        row = rows[0]
        created_at = row.get("created_at")
        completed_at = row.get("completed_at")
        return {
            "command_id": row.get("command_id"),
            "command_type": row.get("command_type"),
            "target_account_key": row.get("target_account_key"),
            "target_account_alias": row.get("target_account_alias"),
            "status": row.get("status"),
            "action_id": row.get("action_id"),
            "actor": row.get("actor"),
            "reason": row.get("reason"),
            "idempotency_key": row.get("idempotency_key"),
            "attempt_count": row.get("attempt_count"),
            "last_error_code": row.get("last_error_code"),
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
            "completed_at": completed_at.isoformat() if hasattr(completed_at, "isoformat") else completed_at,
            "audit_id": row.get("audit_id"),
        }

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
       duration_ms, error_message, request_payload, response_payload, account_key
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

    def fetch_trace_operations_by_trace_id(
        self,
        *,
        account_alias: str,
        trace_id: str,
        limit: int = 100,
    ) -> List[dict[str, Any]]:
        sql = """
SELECT recorded_at, operation_id, account_alias, command_type, status,
       symbol, side, order_kind, volume, ticket, order_id, deal_id, magic,
       duration_ms, error_message, request_payload, response_payload, account_key
FROM trade_command_audits
WHERE account_alias = %s
  AND (
        COALESCE(request_payload->>'trace_id', '') = %s
        OR COALESCE(response_payload->>'trace_id', '') = %s
      )
ORDER BY recorded_at ASC
LIMIT %s
"""
        return self._fetch_dicts(
            sql,
            [
                account_alias,
                trace_id,
                trace_id,
                max(1, int(limit)),
            ],
        )

    def _fetch_dicts(self, sql: str, params: List[Any]) -> List[dict[str, Any]]:
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in rows]

    @staticmethod
    def _normalize_audit_row(row: dict[str, Any]) -> dict[str, Any]:
        recorded_at = row.get("recorded_at")
        request_payload = row.get("request_payload") or {}
        response_payload = row.get("response_payload") or {}
        operation_id = row.get("operation_id")
        return {
            "recorded_at": recorded_at.isoformat() if hasattr(recorded_at, "isoformat") else recorded_at,
            "operation_id": operation_id,
            "audit_id": operation_id,
            "account_alias": row.get("account_alias"),
            "account_key": row.get("account_key"),
            "command_type": row.get("command_type"),
            "status": row.get("status"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "order_kind": row.get("order_kind"),
            "volume": row.get("volume"),
            "ticket": row.get("ticket"),
            "order_id": row.get("order_id"),
            "deal_id": row.get("deal_id"),
            "magic": row.get("magic"),
            "duration_ms": row.get("duration_ms"),
            "error_message": row.get("error_message"),
            "request_payload": request_payload,
            "response_payload": response_payload,
            "trace_id": row.get("trace_id"),
            "signal_id": row.get("signal_id"),
            "actor": row.get("actor") or request_payload.get("actor"),
            "request_id": row.get("request_id"),
            "action_id": row.get("action_id")
                or request_payload.get("action_id")
                or response_payload.get("action_id"),
            "idempotency_key": row.get("idempotency_key")
                or request_payload.get("idempotency_key")
                or response_payload.get("idempotency_key"),
            "reason": row.get("reason") or request_payload.get("reason"),
            "message": (
                response_payload.get("message")
                or response_payload.get("status")
                or row.get("error_message")
            ),
        }
