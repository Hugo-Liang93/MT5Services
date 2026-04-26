from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Iterable, List, Tuple

from src.persistence.schema import (
    INSERT_EXECUTION_INTENTS_SQL,
    INSERT_INTENT_IDEMPOTENCY_KEY_SQL,
)

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
            payload = row[9] if len(row) > 9 and row[9] is not None else {}
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
                    row[8],
                    self._writer._json(payload),
                    row[10],
                    int(row[11] or 0),
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

    def write_intents_with_idempotency(
        self, items: list[dict[str, Any]]
    ) -> list[str]:
        """§0dn B3 + §0do P1：ledger reserve + 主表 INSERT 真正 atomic。

        必须用 ``transaction()`` 而非 ``connection()`` —— ``connection()``
        强制 autocommit=True，每条 cur.execute() 立即 commit 无事务保护
        (§0do P1 修复)；``transaction()`` 显式 BEGIN/COMMIT/ROLLBACK：

          1. INSERT INTO execution_intent_idempotency ON CONFLICT DO NOTHING
          2. rowcount=1 → INSERT INTO execution_intents 主表（同事务）
          3. 全部完成后 commit；任一 INSERT 抛异常 → transaction rollback
             整体回滚，ledger 与主表都保持原状（无幽灵反向）。

        items: list of {intent_key, intent_id, created_at, signal_id,
                        target_account_key, row}
        返回：成功 reserve + commit 的 intent_key 列表。
        """
        if not items:
            return []
        reserved: list[str] = []
        with self._writer.transaction() as conn, conn.cursor() as cur:
            for item in items:
                cur.execute(
                    INSERT_INTENT_IDEMPOTENCY_KEY_SQL,
                    [
                        item["intent_key"],
                        item["intent_id"],
                        item["created_at"],
                        item["signal_id"],
                        item["target_account_key"],
                    ],
                )
                if cur.rowcount > 0:
                    # ledger reserve 成功 → 必须同事务写主表；任一失败回滚
                    row = item["row"]
                    payload = (
                        row[9] if len(row) > 9 and row[9] is not None else {}
                    )
                    decision_metadata = (
                        row[20] if len(row) > 20 and row[20] is not None else {}
                    )
                    cur.execute(
                        INSERT_EXECUTION_INTENTS_SQL,
                        [
                            row[0],
                            row[1],
                            row[2],
                            row[3],
                            row[4],
                            row[5],
                            row[6],
                            row[7],
                            row[8],
                            self._writer._json(payload),
                            row[10],
                            int(row[11] or 0),
                            row[12],
                            row[13],
                            row[14],
                            row[15],
                            row[16],
                            row[17],
                            row[18],
                            row[19],
                            self._writer._json(decision_metadata),
                        ],
                    )
                    reserved.append(item["intent_key"])
        return reserved

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
            # §0dm P1：仅 'claimed' 过期可 reset 为 pending（claimed 状态 broker
            # 尚未 dispatch，retry 安全）；'dispatched' 状态绝不 reset——broker
            # 可能已下单，重新 reclaim 会真实重复执行。dispatched 过期直接进
            # dead_lettered，由人工 reconcile 核对 broker 真实成交结果。
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
            # §0dm P1 + §0do P3：dispatched 过期 → dead_lettered（at-most-once
            # 禁止重放）。RETURNING 整行让 consumer 收到并 emit
            # intent_dead_lettered 事件——这是最需人工 reconcile 的 in-flight
            # 过期事件，pipeline/dashboard/SSE 必须可见。
            cur.execute(
                """
UPDATE execution_intents
SET status = 'dead_lettered',
    dead_lettered_at = %s,
    last_error_code = COALESCE(last_error_code, 'dispatched_lease_expired'),
    lease_expires_at = NULL,
    last_heartbeat_at = NULL,
    claimed_by_instance_id = NULL,
    claimed_by_run_id = NULL,
    claimed_at = NULL
WHERE target_account_key = %s
  AND status = 'dispatched'
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
                [now, target_account_key, now],
            )
            dispatched_dead_rows = cur.fetchall()
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
            # §0do P3：合并 dispatched 过期 dead_letter 行，让 consumer
            # 同样 emit intent_dead_lettered 事件（这是最需人工 reconcile
            # 的 in-flight 过期类型，必须 pipeline 可见）。
            dead_lettered_rows = list(dead_lettered_rows) + list(dispatched_dead_rows)
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

    def mark_intent_dispatched(
        self,
        *,
        intent_id: str,
        claimed_by_instance_id: str,
        claimed_by_run_id: str,
    ) -> bool:
        """§0dm P1：claimed → dispatched atomic CAS 转换。

        在 broker dispatch 之前调用——transition 成功才允许真实下单。
        UPDATE 影响 0 行说明 lease 已被其他 worker 抢走（status 已不是
        'claimed' 或 owner 不匹配），调用方必须放弃 dispatch。

        dispatched 状态后续由 complete_* 终结，或被 reclaim 走 dead_letter
        路径（绝不重置为 pending），避免真实重复下单。
        """
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
UPDATE execution_intents
SET status = 'dispatched'
WHERE intent_id = %s
  AND claimed_by_instance_id = %s
  AND claimed_by_run_id = %s
  AND status = 'claimed'
""",
                [intent_id, claimed_by_instance_id, claimed_by_run_id],
            )
            return cur.rowcount > 0

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
        claimed_by_instance_id: str,
        claimed_by_run_id: str,
        decision_metadata: dict[str, Any] | None = None,
        last_error_code: str | None = None,
    ) -> bool:
        """完成 execution intent 回写，按 owner + status ∈ ('claimed','dispatched')
        校验防覆盖。

        §0dj P2 + §0dm P1：必填 owner + status 谓词；status 谓词扩为
        ('claimed','dispatched')——consumer 进 process_event 前 transition
        到 dispatched，complete 时该 owner 且状态 ∈ {claimed, dispatched}
        即合法终结。UPDATE 0 行说明 lease 已被接管，回写丢弃。

        返回：成功 True，0 行 False。
        """
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
  AND claimed_by_instance_id = %s
  AND claimed_by_run_id = %s
  AND status IN ('claimed', 'dispatched')
""",
                [
                    normalized_status,
                    normalized_status,
                    now,
                    dead_lettered_at,
                    last_error_code,
                    self._writer._json(decision_metadata or {}),
                    intent_id,
                    claimed_by_instance_id,
                    claimed_by_run_id,
                ],
            )
            return cur.rowcount > 0

    def _fetch_dicts(self, sql: str, params: List[Any]) -> List[dict[str, Any]]:
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in rows]
