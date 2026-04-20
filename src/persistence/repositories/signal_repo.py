from __future__ import annotations

from datetime import datetime as _dt
from datetime import timezone as _tz
from typing import TYPE_CHECKING, Any, Iterable, List, Optional, Tuple

from src.persistence.schema import (
    INSERT_AUTO_EXECUTIONS_SQL,
    INSERT_SIGNAL_EVENTS_SQL,
    INSERT_SIGNAL_OUTCOMES_SQL,
    INSERT_SIGNAL_PREVIEW_EVENTS_SQL,
    INSERT_TRADE_OUTCOMES_SQL,
    SIGNAL_OUTCOMES_EXPECTANCY_SQL,
    SIGNAL_OUTCOMES_WINRATE_SQL,
    UPDATE_SIGNAL_ADMISSION_SQL,
)

# 用于 query_signal_events 的合法 sort token，与 /v1/signals/recent 对齐
_SIGNAL_SORT_TOKENS: dict[str, str] = {
    "generated_at_desc": "generated_at DESC",
    "generated_at_asc": "generated_at ASC",
    "priority_desc": "priority DESC NULLS LAST, generated_at DESC",
    "priority_asc": "priority ASC NULLS LAST, generated_at DESC",
    "desc": "generated_at DESC",
    "asc": "generated_at ASC",
}

if TYPE_CHECKING:
    from src.persistence.db import TimescaleWriter


class SignalEventRepository:
    def __init__(self, writer: "TimescaleWriter"):
        self._writer = writer

    def write_signal_events(self, rows: Iterable[Tuple], page_size: int = 200) -> None:
        self._write_signal_rows(INSERT_SIGNAL_EVENTS_SQL, rows, page_size=page_size)

    def write_signal_preview_events(
        self, rows: Iterable[Tuple], page_size: int = 200
    ) -> None:
        self._write_signal_rows(
            INSERT_SIGNAL_PREVIEW_EVENTS_SQL, rows, page_size=page_size
        )

    def _write_signal_rows(
        self, sql: str, rows: Iterable[Tuple], page_size: int = 200
    ) -> None:
        batch = []
        for row in rows:
            used_indicators = row[8] if row[8] is not None else []
            indicators_snapshot = row[9] if row[9] is not None else {}
            metadata = row[10] if row[10] is not None else {}
            batch.append(
                (
                    *row[:8],
                    self._writer._json(used_indicators),
                    self._writer._json(indicators_snapshot),
                    self._writer._json(metadata),
                )
            )
        if not batch:
            return
        self._writer._batch(sql, batch, page_size=page_size)

    def fetch_signal_events(
        self,
        *,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        strategy: Optional[str] = None,
        direction: Optional[str] = None,
        limit: int = 200,
    ) -> List[Tuple]:
        return self._fetch_signal_rows(
            table_name="signal_events",
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            direction=direction,
            limit=limit,
        )

    def fetch_signal_preview_events(
        self,
        *,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        strategy: Optional[str] = None,
        direction: Optional[str] = None,
        limit: int = 200,
    ) -> List[Tuple]:
        return self._fetch_signal_rows(
            table_name="signal_preview_events",
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            direction=direction,
            limit=limit,
        )

    def fetch_signal_event_by_id(
        self,
        *,
        signal_id: str,
        scope: str = "confirmed",
    ) -> Optional[dict[str, Any]]:
        table_name = (
            "signal_preview_events"
            if str(scope or "").strip().lower() == "preview"
            else "signal_events"
        )
        rows = self._fetch_dict_rows(
            "SELECT generated_at, signal_id, symbol, timeframe, strategy, direction, confidence, reason, "
            "used_indicators, indicators_snapshot, metadata "
            f"FROM {table_name} WHERE signal_id = %s LIMIT 1",
            [signal_id],
        )
        return rows[0] if rows else None

    def fetch_signal_events_by_trace_id(
        self,
        *,
        trace_id: str,
        scope: str = "confirmed",
        limit: int = 50,
    ) -> List[dict[str, Any]]:
        table_name = (
            "signal_preview_events"
            if str(scope or "").strip().lower() == "preview"
            else "signal_events"
        )
        return self._fetch_dict_rows(
            "SELECT generated_at, signal_id, symbol, timeframe, strategy, direction, confidence, reason, "
            "used_indicators, indicators_snapshot, metadata "
            f"FROM {table_name} "
            "WHERE COALESCE(metadata->>'signal_trace_id', '') = %s "
            "ORDER BY generated_at ASC "
            "LIMIT %s",
            [trace_id, max(1, int(limit))],
        )

    def _fetch_signal_rows(
        self,
        *,
        table_name: str,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        strategy: Optional[str] = None,
        direction: Optional[str] = None,
        limit: int = 200,
    ) -> List[Tuple]:
        sql = (
            "SELECT generated_at, signal_id, symbol, timeframe, strategy, direction, confidence, reason, "
            "used_indicators, indicators_snapshot, metadata "
            f"FROM {table_name} WHERE 1=1"
        )
        params: List = []
        if symbol is not None:
            sql += " AND symbol = %s"
            params.append(symbol)
        if timeframe is not None:
            sql += " AND timeframe = %s"
            params.append(timeframe)
        if strategy is not None:
            sql += " AND strategy = %s"
            params.append(strategy)
        if direction is not None:
            sql += " AND direction = %s"
            params.append(direction)
        sql += " ORDER BY generated_at DESC LIMIT %s"
        params.append(max(1, int(limit)))
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def query_signal_events(
        self,
        *,
        scope: str = "confirmed",
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        strategy: Optional[str] = None,
        direction: Optional[str] = None,
        status: Optional[str] = None,
        actionability: Optional[str] = None,
        from_time: Optional[_dt] = None,
        to_time: Optional[_dt] = None,
        page: int = 1,
        page_size: int = 200,
        sort: str = "generated_at_desc",
    ) -> dict[str, Any]:
        normalized_scope = str(scope or "confirmed").strip().lower()
        if normalized_scope not in {"confirmed", "preview", "all"}:
            raise ValueError(f"unsupported signal scope: {scope}")

        sort_token = str(sort or "generated_at_desc").strip().lower()
        order_clause = _SIGNAL_SORT_TOKENS.get(sort_token, "generated_at DESC")
        # signal_id 作为 tiebreaker；priority 排序时不附加（因为已在 order_clause 内已含 generated_at）
        if "priority" not in order_clause:
            order_clause = f"{order_clause}, signal_id DESC"
        # preview / all scope 不带 admission 字段（preview 信号永远不进 executor）
        scope_supports_admission = normalized_scope == "confirmed"
        admission_columns = (
            "actionability, guard_reason_code, guard_category, priority, rank_source"
            if scope_supports_admission
            else "NULL::text AS actionability, NULL::text AS guard_reason_code, "
            "NULL::text AS guard_category, NULL::double precision AS priority, "
            "NULL::text AS rank_source"
        )
        effective_page = max(1, int(page))
        effective_page_size = max(1, int(page_size))
        offset = (effective_page - 1) * effective_page_size

        if normalized_scope == "confirmed":
            source_sql = (
                "SELECT generated_at, signal_id, symbol, timeframe, strategy, direction, confidence, reason, "
                "used_indicators, indicators_snapshot, metadata, 'confirmed'::text AS scope, "
                f"{admission_columns} "
                "FROM signal_events"
            )
        elif normalized_scope == "preview":
            source_sql = (
                "SELECT generated_at, signal_id, symbol, timeframe, strategy, direction, confidence, reason, "
                "used_indicators, indicators_snapshot, metadata, 'preview'::text AS scope, "
                f"{admission_columns} "
                "FROM signal_preview_events"
            )
        else:
            source_sql = f"""
SELECT generated_at, signal_id, symbol, timeframe, strategy, direction, confidence, reason,
       used_indicators, indicators_snapshot, metadata, 'confirmed'::text AS scope,
       actionability, guard_reason_code, guard_category, priority, rank_source
FROM signal_events
UNION ALL
SELECT generated_at, signal_id, symbol, timeframe, strategy, direction, confidence, reason,
       used_indicators, indicators_snapshot, metadata, 'preview'::text AS scope,
       NULL::text AS actionability, NULL::text AS guard_reason_code,
       NULL::text AS guard_category, NULL::double precision AS priority,
       NULL::text AS rank_source
FROM signal_preview_events
"""

        sql = f"""
SELECT generated_at,
       signal_id,
       symbol,
       timeframe,
       strategy,
       direction,
       confidence,
       reason,
       used_indicators,
       indicators_snapshot,
       metadata,
       scope,
       actionability,
       guard_reason_code,
       guard_category,
       priority,
       rank_source,
       COUNT(*) OVER() AS total_count
FROM ({source_sql}) AS signal_rows
WHERE 1=1
"""
        params: list[Any] = []
        if symbol is not None:
            sql += " AND symbol = %s"
            params.append(symbol)
        if timeframe is not None:
            sql += " AND timeframe = %s"
            params.append(timeframe)
        if strategy is not None:
            sql += " AND strategy = %s"
            params.append(strategy)
        if direction is not None:
            sql += " AND direction = %s"
            params.append(direction)
        if status is not None:
            sql += " AND COALESCE(metadata->>'signal_state', '') = %s"
            params.append(status)
        if actionability is not None:
            sql += " AND actionability = %s"
            params.append(actionability)
        if from_time is not None:
            sql += " AND generated_at >= %s"
            params.append(from_time)
        if to_time is not None:
            sql += " AND generated_at <= %s"
            params.append(to_time)
        sql += f" ORDER BY {order_clause} LIMIT %s OFFSET %s"
        params.extend([effective_page_size, offset])

        rows = self._fetch_dict_rows(sql, params)
        total = int(rows[0].get("total_count") or 0) if rows else 0
        items = [self._dict_row_to_event(row) for row in rows]
        return {
            "items": items,
            "total": total,
            "page": effective_page,
            "page_size": effective_page_size,
            "scope": normalized_scope,
        }

    def update_signal_admission(
        self,
        *,
        signal_id: str,
        actionability: str,
        guard_reason_code: Optional[str],
        guard_category: Optional[str],
        priority: Optional[float],
        rank_source: str = "native",
    ) -> bool:
        """回填 executor admission 结果到 signal_events。

        返回 True 表示更新生效（rowcount > 0）；False 表示 signal_id 不存在。
        异常向上抛出（由 listener 层 try/except 隔离），不在此处吞错。
        """
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(
                UPDATE_SIGNAL_ADMISSION_SQL,
                [
                    actionability,
                    guard_reason_code,
                    guard_category,
                    priority,
                    rank_source,
                    signal_id,
                ],
            )
            return cur.rowcount > 0

    def fetch_signal_confidence(self, *, signal_id: str) -> Optional[float]:
        """读取已持久化 signal 的 confidence，用于 listener 计算 priority。

        若 signal_id 不存在返回 None。signal_events 表中查找。
        """
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT confidence FROM signal_events WHERE signal_id = %s LIMIT 1",
                [signal_id],
            )
            row = cur.fetchone()
        if not row:
            return None
        try:
            return float(row[0]) if row[0] is not None else None
        except (TypeError, ValueError):
            return None

    def summarize_signal_events(self, *, hours: int = 24) -> List[Tuple]:
        return self._summarize_signal_rows(table_name="signal_events", hours=hours)

    def summarize_signal_preview_events(self, *, hours: int = 24) -> List[Tuple]:
        return self._summarize_signal_rows(
            table_name="signal_preview_events", hours=hours
        )

    def _summarize_signal_rows(
        self, *, table_name: str, hours: int = 24
    ) -> List[Tuple]:
        sql = (
            "SELECT symbol, timeframe, strategy, direction, COUNT(*)::bigint AS count, "
            "AVG(confidence)::double precision AS avg_confidence, MAX(generated_at) AS last_seen_at "
            f"FROM {table_name} "
            "WHERE generated_at >= NOW() - (%s * INTERVAL '1 hour') "
            "GROUP BY symbol, timeframe, strategy, direction "
            "ORDER BY symbol, timeframe, strategy, direction"
        )
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, [max(1, int(hours))])
            return cur.fetchall()

    def write_outcome_events(self, rows: Iterable[Tuple], page_size: int = 200) -> None:
        batch = []
        for row in rows:
            metadata = row[13] if len(row) > 13 and row[13] is not None else {}
            batch.append((*row[:13], self._writer._json(metadata)))
        if not batch:
            return
        self._writer._batch(INSERT_SIGNAL_OUTCOMES_SQL, batch, page_size=page_size)

    def write_auto_executions(self, rows: Iterable[dict], page_size: int = 200) -> None:
        batch = []
        for entry in rows:
            params = entry.get("params") or {}
            try:
                executed_at = _dt.fromisoformat(str(entry.get("at") or "")).replace(
                    tzinfo=_tz.utc
                )
            except (ValueError, TypeError):
                executed_at = _dt.now(_tz.utc)
            batch.append(
                (
                    executed_at,
                    entry.get("signal_id") or "",
                    entry.get("account_key"),
                    entry.get("account_alias"),
                    entry.get("intent_id"),
                    entry.get("symbol") or "",
                    entry.get("direction") or "",
                    entry.get("strategy") or "",
                    entry.get("confidence"),
                    params.get("volume"),
                    params.get("entry_price"),
                    params.get("sl"),
                    params.get("tp"),
                    params.get("rr"),
                    bool(entry.get("success")),
                    entry.get("error"),
                    self._writer._json(
                        {
                            "cost": entry.get("cost") or {},
                            "execution_quality": entry.get("execution_quality") or {},
                            **(entry.get("metadata") or {}),
                        }
                    ),
                )
            )
        if not batch:
            return
        self._writer._batch(INSERT_AUTO_EXECUTIONS_SQL, batch, page_size=page_size)

    def fetch_winrates(
        self,
        *,
        hours: int = 168,
        symbol: Optional[str] = None,
    ) -> List[Tuple]:
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(SIGNAL_OUTCOMES_WINRATE_SQL, [max(1, hours), symbol, symbol])
            return cur.fetchall()

    def write_trade_outcomes(self, rows: Iterable[Tuple], page_size: int = 200) -> None:
        batch = []
        for row in rows:
            metadata = row[15] if len(row) > 15 and row[15] is not None else {}
            batch.append((*row[:15], self._writer._json(metadata)))
        if not batch:
            return
        self._writer._batch(INSERT_TRADE_OUTCOMES_SQL, batch, page_size=page_size)

    def fetch_expectancy_stats(
        self,
        *,
        hours: int = 168,
        symbol: Optional[str] = None,
    ) -> List[Tuple]:
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(
                SIGNAL_OUTCOMES_EXPECTANCY_SQL,
                [max(1, hours), symbol, symbol],
            )
            return cur.fetchall()

    def fetch_recent_signal_outcomes(self, *, hours: int = 24) -> List[dict]:
        return self._fetch_recent_outcomes(
            """
SELECT strategy,
       won,
       COALESCE(price_change, 0.0) AS pnl,
       regime,
       'signal'                    AS source,
       recorded_at
FROM signal_outcomes
WHERE recorded_at >= NOW() - (%s * INTERVAL '1 hour')
  AND won IS NOT NULL
ORDER BY recorded_at ASC
""",
            [max(1, int(hours))],
        )

    def fetch_recent_trade_outcomes(
        self,
        *,
        hours: int = 24,
        account_key: str | None = None,
    ) -> List[dict]:
        sql = """
SELECT strategy,
       won,
       COALESCE(price_change, 0.0) AS pnl,
       regime,
       'trade'                     AS source,
       recorded_at
FROM trade_outcomes
WHERE recorded_at >= NOW() - (%s * INTERVAL '1 hour')
  AND won IS NOT NULL
"""
        params: list[Any] = [max(1, int(hours))]
        if account_key is not None:
            sql += " AND account_key = %s"
            params.append(account_key)
        sql += " ORDER BY recorded_at ASC"
        return self._fetch_recent_outcomes(sql, params)

    def fetch_recent_outcomes(self, *, hours: int = 24) -> List[dict]:
        sql = """
SELECT strategy,
       won,
       COALESCE(price_change, 0.0) AS pnl,
       regime,
       'signal'                    AS source,
       recorded_at
FROM signal_outcomes
WHERE recorded_at >= NOW() - (%s * INTERVAL '1 hour')
  AND won IS NOT NULL
UNION ALL
SELECT strategy,
       won,
       COALESCE(price_change, 0.0) AS pnl,
       regime,
       'trade'                     AS source,
       recorded_at
FROM trade_outcomes
WHERE recorded_at >= NOW() - (%s * INTERVAL '1 hour')
  AND won IS NOT NULL
ORDER BY recorded_at ASC
"""
        h = max(1, int(hours))
        return self._fetch_recent_outcomes(sql, [h, h])

    def fetch_auto_executions(
        self,
        *,
        signal_id: str,
        limit: int = 50,
    ) -> List[dict[str, Any]]:
        return self._fetch_dict_rows(
            """
SELECT executed_at, signal_id, symbol, direction, strategy, confidence,
       account_key, account_alias, intent_id, volume, entry_price, stop_loss, take_profit, risk_reward,
       success, error_message, metadata
FROM auto_executions
WHERE signal_id = %s
ORDER BY executed_at ASC
LIMIT %s
""",
            [signal_id, max(1, int(limit))],
        )

    def fetch_signal_outcomes(
        self,
        *,
        signal_id: str,
        limit: int = 20,
    ) -> List[dict[str, Any]]:
        return self._fetch_dict_rows(
            """
SELECT recorded_at, signal_id, symbol, timeframe, strategy, direction, confidence,
       entry_price, exit_price, price_change, won, bars_held, regime, metadata
FROM signal_outcomes
WHERE signal_id = %s
ORDER BY recorded_at ASC
LIMIT %s
""",
            [signal_id, max(1, int(limit))],
        )

    def fetch_trade_outcomes(
        self,
        *,
        signal_id: str,
        limit: int = 20,
    ) -> List[dict[str, Any]]:
        return self._fetch_dict_rows(
            """
SELECT recorded_at, signal_id, symbol, timeframe, strategy, direction, confidence,
       account_key, account_alias, intent_id, fill_price, close_price, price_change, won, regime, metadata
FROM trade_outcomes
WHERE signal_id = %s
ORDER BY recorded_at ASC
LIMIT %s
""",
            [signal_id, max(1, int(limit))],
        )

    def _fetch_dict_rows(self, sql: str, params: list[Any]) -> List[dict[str, Any]]:
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in rows]

    def _fetch_recent_outcomes(self, sql: str, params: list[Any]) -> List[dict]:
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [
            {
                "strategy": r[0],
                "won": r[1],
                "pnl": float(r[2]) if r[2] is not None else 0.0,
                "regime": r[3],
                "source": r[4],
                "recorded_at": r[5],
            }
            for r in rows
        ]

    @staticmethod
    def _dict_row_to_event(row: dict[str, Any]) -> dict[str, Any]:
        metadata = row.get("metadata") or {}
        generated_at = row.get("generated_at")
        return {
            "generated_at": (
                generated_at.isoformat()
                if hasattr(generated_at, "isoformat")
                else generated_at
            ),
            "signal_id": row.get("signal_id"),
            "symbol": row.get("symbol"),
            "timeframe": row.get("timeframe"),
            "strategy": row.get("strategy"),
            "direction": row.get("direction"),
            "confidence": row.get("confidence"),
            "reason": row.get("reason"),
            "used_indicators": row.get("used_indicators") or [],
            "indicators_snapshot": row.get("indicators_snapshot") or {},
            "metadata": metadata,
            "signal_state": metadata.get("signal_state"),
            "scope": row.get("scope") or "confirmed",
            # P9 Phase 1.5: admission writeback 字段（旧记录可能为 NULL）
            "actionability": row.get("actionability"),
            "guard_reason_code": row.get("guard_reason_code"),
            "guard_category": row.get("guard_category"),
            "priority": row.get("priority"),
            "rank_source": row.get("rank_source"),
        }
