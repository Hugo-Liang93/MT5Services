"""Repository for entry_policy_decisions audit table (ADR-013)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterable, List, Optional, Sequence, Tuple

from src.persistence.schema import (
    UPDATE_ENTRY_POLICY_DECISION_FILL_SQL,
    UPSERT_ENTRY_POLICY_DECISIONS_SQL,
)

if TYPE_CHECKING:
    from src.persistence.db import TimescaleWriter


class EntryPolicyDecisionRepository:
    """读写 entry_policy_decisions 表。

    write_decisions(rows): 批量 upsert。row 顺序与 UPSERT_SQL 占位符一致。
    update_fill_outcome(...): fill / cancel 时回填终态字段。
    fetch_decisions(...): 按 strategy/tf/policy 切片读。
    """

    def __init__(self, writer: "TimescaleWriter"):
        self._writer = writer

    def write_decisions(
        self,
        rows: Iterable[Tuple],
        page_size: int = 200,
    ) -> None:
        batch = []
        for row in rows:
            # row[10] = members (list/dict 转 JSONB)；row[16] = metadata
            members = row[10] if row[10] is not None else []
            metadata = row[16] if row[16] is not None else {}
            batch.append(
                (
                    *row[:10],
                    self._writer._json(members),
                    *row[11:16],
                    self._writer._json(metadata),
                    row[17],
                )
            )
        if not batch:
            return
        self._writer._batch(
            UPSERT_ENTRY_POLICY_DECISIONS_SQL, batch, page_size=page_size
        )

    def update_fill_outcome(
        self,
        *,
        account_key: str,
        group_id: str,
        fill_member_id: Optional[str],
        fill_at: Optional[datetime],
        fill_price: Optional[float],
        fill_outcome: str,
    ) -> None:
        with self._writer.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    UPDATE_ENTRY_POLICY_DECISION_FILL_SQL,
                    (
                        fill_member_id,
                        fill_at,
                        fill_price,
                        fill_outcome,
                        account_key,
                        group_id,
                    ),
                )

    def fetch_decisions(
        self,
        *,
        account_key: Optional[str] = None,
        strategy: Optional[str] = None,
        timeframe: Optional[str] = None,
        policy_name: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: int = 200,
    ) -> List[dict]:
        sql = (
            "SELECT account_key, signal_id, strategy, timeframe, direction, pattern_type, "
            "policy_name, branch, group_id, cancellation_policy, members, "
            "decided_at, fill_member_id, fill_at, fill_price, fill_outcome, "
            "metadata, updated_at "
            "FROM entry_policy_decisions WHERE 1=1"
        )
        params: List[Any] = []
        if account_key is not None:
            sql += " AND account_key = %s"
            params.append(account_key)
        if strategy is not None:
            sql += " AND strategy = %s"
            params.append(strategy)
        if timeframe is not None:
            sql += " AND timeframe = %s"
            params.append(timeframe)
        if policy_name is not None:
            sql += " AND policy_name = %s"
            params.append(policy_name)
        if since is not None:
            sql += " AND decided_at >= %s"
            params.append(since)
        sql += " ORDER BY decided_at DESC LIMIT %s"
        params.append(limit)

        with self._writer.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [desc[0] for desc in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
