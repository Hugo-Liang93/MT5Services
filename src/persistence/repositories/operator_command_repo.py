from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Iterable

from src.persistence.schema import (
    INSERT_COMMAND_IDEMPOTENCY_KEY_SQL,
    INSERT_OPERATOR_COMMANDS_SQL,
)

if TYPE_CHECKING:
    from src.persistence.db import TimescaleWriter


class OperatorCommandRepository:
    def __init__(self, writer: "TimescaleWriter") -> None:
        self._writer = writer

    def write_operator_commands(
        self,
        rows: Iterable[tuple[Any, ...]],
        page_size: int = 200,
    ) -> None:
        batch: list[tuple[Any, ...]] = []
        for row in rows:
            request_context = row[10] if len(row) > 10 and row[10] is not None else {}
            payload = row[11] if len(row) > 11 and row[11] is not None else {}
            response_payload = row[20] if len(row) > 20 and row[20] is not None else {}
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
                    row[9],
                    self._writer._json(request_context),
                    self._writer._json(payload),
                    row[12],
                    row[13],
                    row[14],
                    row[15],
                    row[16],
                    row[17],
                    row[18],
                    row[19],
                    self._writer._json(response_payload),
                    row[21],
                )
            )
        if not batch:
            return
        self._writer._batch(INSERT_OPERATOR_COMMANDS_SQL, batch, page_size=page_size)

    def claim_operator_commands(
        self,
        *,
        target_account_key: str,
        claimed_by_instance_id: str,
        claimed_by_run_id: str,
        limit: int = 20,
        lease_seconds: int = 30,
        max_attempts: int = 5,
    ) -> dict[str, list[dict[str, Any]]]:
        now = datetime.now(timezone.utc)
        lease_expires_at = now + timedelta(seconds=max(5, int(lease_seconds)))
        with self._writer.connection() as conn, conn.cursor() as cur:
            # §0dm P2：仅 'claimed' 过期可 reset 为 pending（claimed 状态命令
            # 尚未真实执行 → retry 安全）；'dispatched' 状态绝不 reset——命令
            # 副作用（close_all_positions/cancel_orders 等）已开始或已完成，
            # 重新 reclaim 会污染审计 + 重复执行。dispatched 过期 → dead_letter。
            cur.execute(
                """
UPDATE operator_commands
SET status = 'pending',
    claimed_by_instance_id = NULL,
    claimed_by_run_id = NULL,
    claimed_at = NULL,
    lease_expires_at = NULL,
    last_heartbeat_at = NULL
WHERE target_account_key = %s
  AND status = 'claimed'
  AND lease_expires_at IS NOT NULL
  AND lease_expires_at < NOW()
RETURNING created_at,
          command_id,
          command_type,
          target_account_key,
          target_account_alias,
          status,
          action_id,
          actor,
          reason,
          idempotency_key,
          request_context,
          payload,
          claimed_by_instance_id,
          claimed_by_run_id,
          claimed_at,
          lease_expires_at,
          last_heartbeat_at,
          attempt_count,
          last_error_code,
          completed_at,
          response_payload,
          audit_id
""",
                [target_account_key],
            )
            reclaimed_rows = cur.fetchall()
            reclaimed_columns = [desc[0] for desc in cur.description]
            # §0dm P2：dispatched 过期 → dead_letter（at-most-once：禁止重放副作用）
            cur.execute(
                """
UPDATE operator_commands
SET status = 'dead_lettered',
    dead_lettered_at = NOW(),
    completed_at = NOW(),
    last_error_code = COALESCE(last_error_code, 'dispatched_lease_expired'),
    lease_expires_at = NULL,
    last_heartbeat_at = NULL,
    claimed_by_instance_id = NULL,
    claimed_by_run_id = NULL,
    claimed_at = NULL
WHERE target_account_key = %s
  AND status = 'dispatched'
  AND lease_expires_at IS NOT NULL
  AND lease_expires_at < NOW()
""",
                [target_account_key],
            )
            cur.execute(
                """
UPDATE operator_commands
SET status = 'dead_lettered',
    dead_lettered_at = NOW(),
    completed_at = NOW(),
    last_error_code = COALESCE(last_error_code, 'command_attempts_exhausted')
WHERE target_account_key = %s
  AND status IN ('pending', 'claimed')
  AND COALESCE(attempt_count, 0) >= %s
  AND (status = 'pending' OR lease_expires_at < NOW())
RETURNING created_at,
          command_id,
          command_type,
          target_account_key,
          target_account_alias,
          status,
          action_id,
          actor,
          reason,
          idempotency_key,
          request_context,
          payload,
          claimed_by_instance_id,
          claimed_by_run_id,
          claimed_at,
          lease_expires_at,
          last_heartbeat_at,
          attempt_count,
          last_error_code,
          completed_at,
          response_payload,
          audit_id
""",
                [target_account_key, max(1, int(max_attempts))],
            )
            dead_lettered_rows = cur.fetchall()
            dead_lettered_columns = [desc[0] for desc in cur.description]
            cur.execute(
                """
WITH locked AS (
    SELECT created_at, command_id
    FROM operator_commands
    WHERE target_account_key = %s
      AND status = 'pending'
      AND COALESCE(attempt_count, 0) < %s
    ORDER BY created_at ASC
    LIMIT %s
    FOR UPDATE SKIP LOCKED
)
UPDATE operator_commands commands
SET status = 'claimed',
    claimed_by_instance_id = %s,
    claimed_by_run_id = %s,
    claimed_at = %s,
    lease_expires_at = %s,
    last_heartbeat_at = %s,
    attempt_count = COALESCE(commands.attempt_count, 0) + 1
FROM locked
WHERE commands.created_at = locked.created_at
  AND commands.command_id = locked.command_id
RETURNING commands.created_at,
          commands.command_id,
          commands.command_type,
          commands.target_account_key,
          commands.target_account_alias,
          commands.status,
          commands.action_id,
          commands.actor,
          commands.reason,
          commands.idempotency_key,
          commands.request_context,
          commands.payload,
          commands.claimed_by_instance_id,
          commands.claimed_by_run_id,
          commands.claimed_at,
          commands.lease_expires_at,
          commands.last_heartbeat_at,
          commands.attempt_count,
          commands.last_error_code,
          commands.completed_at,
          commands.response_payload,
          commands.audit_id
""",
                [
                    target_account_key,
                    max(1, int(max_attempts)),
                    max(1, int(limit)),
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

    def write_command_with_idempotency(
        self,
        *,
        target_account_key: str,
        command_type: str,
        idempotency_key: str | None,
        command_id: str,
        created_at: datetime,
        row: tuple,
    ) -> bool:
        """§0dn B3：ledger reserve + 主表 INSERT atomic（同 transaction）。

        idempotency_key=None 时仅写主表（无幂等保护，少数命令场景）；
        有值时同 connection 内嵌套：
          1. INSERT INTO operator_command_idempotency ON CONFLICT DO NOTHING
          2. rowcount=1 → INSERT INTO operator_commands 主表
          3. rowcount=0 → 跳过主表，返 False（caller 查 _find_existing）
        全部完成后 commit；任一 INSERT 抛异常 → rollback。

        返回：True=本 command 已 committed（首次 reserve）；
              False=ledger 命中，主表未写（caller 应查 existing）。
        """
        with self._writer.connection() as conn, conn.cursor() as cur:
            if idempotency_key is not None:
                cur.execute(
                    INSERT_COMMAND_IDEMPOTENCY_KEY_SQL,
                    [
                        target_account_key,
                        command_type,
                        idempotency_key,
                        command_id,
                        created_at,
                    ],
                )
                if cur.rowcount == 0:
                    return False
            # 主表写入（INSERT_OPERATOR_COMMANDS_SQL 期望 22 列）
            request_context = (
                row[10] if len(row) > 10 and row[10] is not None else {}
            )
            payload = row[11] if len(row) > 11 and row[11] is not None else {}
            response_payload = (
                row[20] if len(row) > 20 and row[20] is not None else {}
            )
            cur.execute(
                INSERT_OPERATOR_COMMANDS_SQL,
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
                    row[9],
                    self._writer._json(request_context),
                    self._writer._json(payload),
                    row[12],
                    row[13],
                    row[14],
                    row[15],
                    row[16],
                    int(row[17] or 0),
                    row[18],
                    row[19],
                    self._writer._json(response_payload),
                    row[21],
                ],
            )
            return True

    def mark_command_dispatched(
        self,
        *,
        command_id: str,
        claimed_by_instance_id: str,
        claimed_by_run_id: str,
    ) -> bool:
        """§0dm P2：claimed → dispatched atomic CAS 转换。

        在执行控制面副作用（close_all_positions / cancel_orders /
        set_runtime_mode 等）之前调用——transition 成功才允许真实执行。
        UPDATE 0 行说明 lease 已被其他 consumer 抢走，调用方必须放弃执行。

        dispatched 状态后续由 complete_* 终结，或被 reclaim 走 dead_letter
        路径（绝不重置 pending），避免控制面副作用重放。
        """
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
UPDATE operator_commands
SET status = 'dispatched'
WHERE command_id = %s
  AND claimed_by_instance_id = %s
  AND claimed_by_run_id = %s
  AND status = 'claimed'
""",
                [command_id, claimed_by_instance_id, claimed_by_run_id],
            )
            return cur.rowcount > 0

    def heartbeat_operator_command(
        self,
        *,
        command_id: str,
        claimed_by_instance_id: str,
        claimed_by_run_id: str | None = None,
        lease_seconds: int = 30,
    ) -> None:
        now = datetime.now(timezone.utc)
        lease_expires_at = now + timedelta(seconds=max(5, int(lease_seconds)))
        params: list[Any] = [now, lease_expires_at, command_id, claimed_by_instance_id]
        # §0dn B1：status 谓词扩 ('claimed', 'dispatched')——§0dm 引入
        # dispatched 状态后，process 期间 heartbeat 必须能续 dispatched 行
        # 的 lease（旧实现仅 'claimed' → mark_dispatched 后 heartbeat 0 行
        # 不续，慢命令必然 30s 后超时 dead_letter）。
        sql = """
UPDATE operator_commands
SET last_heartbeat_at = %s,
    lease_expires_at = %s
WHERE command_id = %s
  AND claimed_by_instance_id = %s
  AND status IN ('claimed', 'dispatched')
"""
        if claimed_by_run_id is not None:
            sql += " AND claimed_by_run_id = %s"
            params.append(claimed_by_run_id)
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)

    def complete_operator_command(
        self,
        *,
        command_id: str,
        status: str,
        claimed_by_instance_id: str,
        claimed_by_run_id: str,
        response_payload: dict[str, Any] | None = None,
        audit_id: str | None = None,
        last_error_code: str | None = None,
    ) -> bool:
        """完成 operator command 回写，按 owner + status='claimed' 校验防覆盖。

        §0dj P2：旧实现仅 ``WHERE command_id = %s`` → 慢 consumer 超时被新
        consumer 接管后，迟到的旧 consumer 仍能覆盖新执行的 audit_id 与
        response_payload。修复：必填 owner + status 谓词。

        返回：成功更新返 True，0 行（已被其他 consumer 接管或已完成）返 False。
        """
        completed_at = datetime.now(timezone.utc)
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
UPDATE operator_commands
SET status = %s,
    response_payload = %s,
    audit_id = %s,
    last_error_code = %s,
    completed_at = %s,
    claimed_by_instance_id = NULL,
    claimed_by_run_id = NULL,
    claimed_at = NULL,
    lease_expires_at = NULL,
    last_heartbeat_at = %s
WHERE command_id = %s
  AND claimed_by_instance_id = %s
  AND claimed_by_run_id = %s
  AND status IN ('claimed', 'dispatched')
""",
                [
                    status,
                    self._writer._json(response_payload or {}),
                    audit_id,
                    last_error_code,
                    completed_at,
                    completed_at,
                    command_id,
                    claimed_by_instance_id,
                    claimed_by_run_id,
                ],
            )
            return cur.rowcount > 0

    def fetch_operator_commands(
        self,
        *,
        command_id: str | None = None,
        command_type: str | None = None,
        target_account_key: str | None = None,
        target_account_alias: str | None = None,
        idempotency_key: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if command_id:
            conditions.append("command_id = %s")
            params.append(command_id)
        if command_type:
            conditions.append("command_type = %s")
            params.append(command_type)
        if target_account_key:
            conditions.append("target_account_key = %s")
            params.append(target_account_key)
        if target_account_alias:
            conditions.append("target_account_alias = %s")
            params.append(target_account_alias)
        if idempotency_key:
            conditions.append("idempotency_key = %s")
            params.append(idempotency_key)
        if statuses:
            conditions.append("status = ANY(%s)")
            params.append(list(statuses))
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"""
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
       request_context,
       payload,
       claimed_by_instance_id,
       claimed_by_run_id,
       claimed_at,
       lease_expires_at,
       last_heartbeat_at,
       attempt_count,
       last_error_code,
       completed_at,
       response_payload,
       audit_id
FROM operator_commands
{where}
ORDER BY created_at DESC
LIMIT %s
"""
        params.append(max(1, int(limit)))
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in rows]
