from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Iterable, List, Tuple

from src.persistence.schema import INSERT_EXECUTION_INTENTS_SQL

if TYPE_CHECKING:
    from src.persistence.db import TimescaleWriter


class ExecutionIntentRepository:
    def __init__(self, writer: "TimescaleWriter") -> None:
        self._writer = writer

    def write_execution_intents(
        self,
        rows: Iterable[Tuple[Any, ...]],
        page_size: int = 200,
    ) -> None:
        batch = []
        for row in rows:
            payload = row[8] if len(row) > 8 and row[8] is not None else {}
            decision_metadata = row[20] if len(row) > 20 and row[20] is not None else {}
            batch.append(
                (
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                    self._writer._json(payload),
                    row[9],
                    int(row[10] or 0),
                    row[11],
                    row[12],
                    row[13],
                    row[14],
                    row[15],
                    row[16],
                    row[17],
                    row[18],
                    row[19],
                    self._writer._json(decision_metadata),
                )
            )
        if not batch:
            return
        self._writer._batch(INSERT_EXECUTION_INTENTS_SQL, batch, page_size=page_size)

    def claim_execution_intents(
        self,
        *,
        target_account_key: str,
        claimed_by_instance_id: str,
        claimed_by_run_id: str | None = None,
        limit: int = 20,
        lease_seconds: int = 30,
        max_attempts: int = 5,
    ) -> dict[str, List[dict[str, Any]]]:
        effective_limit = max(1, int(limit))
        effective_lease_seconds = max(5, int(lease_seconds))
        effective_max_attempts = max(1, int(max_attempts))
        now = datetime.now(timezone.utc)
        lease_expires_at = now + timedelta(seconds=effective_lease_seconds)

        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
UPDATE execution_intents
SET status = 'pending',
    claimed_by_instance_id = NULL,
    claimed_by_run_id = NULL,
    claimed_at = NULL,
    lease_expires_at = NULL,
    last_heartbeat_at = NULL
WHERE target_account_key = %s
  AND status = 'claimed'
  AND lease_expires_at IS NOT NULL
  AND lease_expires_at < %s
RETURNING created_at,
          intent_id,
          intent_key,
          signal_id,
          target_account_key,
          target_account_alias,
          strategy,
          symbol,
          timeframe,
          payload,
          status,
          attempt_count,
          claimed_by_instance_id,
          claimed_by_run_id,
          claimed_at,
          lease_expires_at,
          last_heartbeat_at,
          completed_at,
          dead_lettered_at,
          last_error_code,
          decision_metadata
""",
                [target_account_key, now],
            )
            reclaimed_rows = cur.fetchall()
            reclaimed_columns = [desc[0] for desc in cur.description]
            cur.execute(
                """
UPDATE execution_intents
SET status = 'dead_lettered',
    dead_lettered_at = %s,
    lease_expires_at = NULL,
    last_heartbeat_at = NULL,
    claimed_by_instance_id = NULL,
    claimed_by_run_id = NULL,
    claimed_at = NULL
WHERE target_account_key = %s
  AND status = 'pending'
  AND attempt_count >= %s
  AND dead_lettered_at IS NULL
RETURNING created_at,
          intent_id,
          intent_key,
          signal_id,
          target_account_key,
          target_account_alias,
          strategy,
          symbol,
          timeframe,
          payload,
          status,
          attempt_count,
          claimed_by_instance_id,
          claimed_by_run_id,
          claimed_at,
          lease_expires_at,
          last_heartbeat_at,
          completed_at,
          dead_lettered_at,
          last_error_code,
          decision_metadata
""",
                [now, target_account_key, effective_max_attempts],
            )
            dead_lettered_rows = cur.fetchall()
            dead_lettered_columns = [desc[0] for desc in cur.description]
            cur.execute(
                """
WITH locked AS (
    SELECT created_at, intent_id
    FROM execution_intents
    WHERE target_account_key = %s
      AND status = 'pending'
      AND dead_lettered_at IS NULL
      AND attempt_count < %s
    ORDER BY created_at ASC
    LIMIT %s
    FOR UPDATE SKIP LOCKED
)
UPDATE execution_intents intents
SET status = 'claimed',
    attempt_count = intents.attempt_count + 1,
    claimed_by_instance_id = %s,
    claimed_by_run_id = %s,
    claimed_at = %s,
    lease_expires_at = %s,
    last_heartbeat_at = %s
FROM locked
WHERE intents.created_at = locked.created_at
  AND intents.intent_id = locked.intent_id
RETURNING intents.created_at,
          intents.intent_id,
          intents.intent_key,
          intents.signal_id,
          intents.target_account_key,
          intents.target_account_alias,
          intents.strategy,
          intents.symbol,
          intents.timeframe,
          intents.payload,
          intents.status,
          intents.attempt_count,
          intents.claimed_by_instance_id,
          intents.claimed_by_run_id,
          intents.claimed_at,
          intents.lease_expires_at,
          intents.last_heartbeat_at,
          intents.completed_at,
          intents.dead_lettered_at,
          intents.last_error_code,
          intents.decision_metadata
""",
                [
                    target_account_key,
                    effective_max_attempts,
                    effective_limit,
                    claimed_by_instance_id,
                    claimed_by_run_id,
                    now,
                    lease_expires_at,
                    now,
                ],
            )
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
        return {
            "claimed": [dict(zip(columns, row)) for row in rows],
            "reclaimed": [dict(zip(reclaimed_columns, row)) for row in reclaimed_rows],
            "dead_lettered": [
                dict(zip(dead_lettered_columns, row)) for row in dead_lettered_rows
            ],
        }

    def heartbeat_execution_intent(
        self,
        *,
        intent_id: str,
        claimed_by_instance_id: str,
        claimed_by_run_id: str | None = None,
        lease_seconds: int = 30,
    ) -> None:
        now = datetime.now(timezone.utc)
        lease_expires_at = now + timedelta(seconds=max(5, int(lease_seconds)))
        params: list[Any] = [now, lease_expires_at, intent_id, claimed_by_instance_id]
        sql = """
UPDATE execution_intents
SET last_heartbeat_at = %s,
    lease_expires_at = %s
WHERE intent_id = %s
  AND claimed_by_instance_id = %s
"""
        if claimed_by_run_id is not None:
            sql += " AND claimed_by_run_id = %s"
            params.append(claimed_by_run_id)
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)

    def complete_execution_intent(
        self,
        *,
        intent_id: str,
        status: str,
        decision_metadata: dict[str, Any] | None = None,
        last_error_code: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        normalized_status = str(status or "").strip().lower() or "failed"
        dead_lettered_at = now if normalized_status == "dead_lettered" else None
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
UPDATE execution_intents
SET status = %s,
    completed_at = CASE
        WHEN %s IN ('completed', 'failed', 'skipped', 'dead_lettered')
        THEN %s
        ELSE completed_at
    END,
    dead_lettered_at = %s,
    last_error_code = %s,
    decision_metadata = %s,
    claimed_by_instance_id = NULL,
    claimed_by_run_id = NULL,
    claimed_at = NULL,
    lease_expires_at = NULL,
    last_heartbeat_at = NULL
WHERE intent_id = %s
""",
                [
                    normalized_status,
                    normalized_status,
                    now,
                    dead_lettered_at,
                    last_error_code,
                    self._writer._json(decision_metadata or {}),
                    intent_id,
                ],
            )

    def _fetch_dicts(self, sql: str, params: List[Any]) -> List[dict[str, Any]]:
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in rows]
