from __future__ import annotations

from datetime import datetime as _dt, timezone as _tz
from typing import TYPE_CHECKING, Iterable, List, Optional, Tuple

from src.persistence.schema import (
    INSERT_AUTO_EXECUTIONS_SQL,
    INSERT_SIGNAL_EVENTS_SQL,
    INSERT_TRADE_OUTCOMES_SQL,
    SIGNAL_OUTCOMES_EXPECTANCY_SQL,
    INSERT_SIGNAL_OUTCOMES_SQL,
    INSERT_SIGNAL_PREVIEW_EVENTS_SQL,
    SIGNAL_OUTCOMES_WINRATE_SQL,
)

if TYPE_CHECKING:
    from src.persistence.db import TimescaleWriter


class SignalEventRepository:
    def __init__(self, writer: "TimescaleWriter"):
        self._writer = writer

    def write_signal_events(self, rows: Iterable[Tuple], page_size: int = 200) -> None:
        self._write_signal_rows(INSERT_SIGNAL_EVENTS_SQL, rows, page_size=page_size)

    def write_signal_preview_events(self, rows: Iterable[Tuple], page_size: int = 200) -> None:
        self._write_signal_rows(INSERT_SIGNAL_PREVIEW_EVENTS_SQL, rows, page_size=page_size)

    def _write_signal_rows(self, sql: str, rows: Iterable[Tuple], page_size: int = 200) -> None:
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
        action: Optional[str] = None,
        limit: int = 200,
    ) -> List[Tuple]:
        return self._fetch_signal_rows(
            table_name="signal_events",
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            action=action,
            limit=limit,
        )

    def fetch_signal_preview_events(
        self,
        *,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        strategy: Optional[str] = None,
        action: Optional[str] = None,
        limit: int = 200,
    ) -> List[Tuple]:
        return self._fetch_signal_rows(
            table_name="signal_preview_events",
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            action=action,
            limit=limit,
        )

    def _fetch_signal_rows(
        self,
        *,
        table_name: str,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        strategy: Optional[str] = None,
        action: Optional[str] = None,
        limit: int = 200,
    ) -> List[Tuple]:
        sql = (
            "SELECT generated_at, signal_id, symbol, timeframe, strategy, action, confidence, reason, "
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
        if action is not None:
            sql += " AND action = %s"
            params.append(action)
        sql += " ORDER BY generated_at DESC LIMIT %s"
        params.append(max(1, int(limit)))
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def summarize_signal_events(self, *, hours: int = 24) -> List[Tuple]:
        return self._summarize_signal_rows(table_name="signal_events", hours=hours)

    def summarize_signal_preview_events(self, *, hours: int = 24) -> List[Tuple]:
        return self._summarize_signal_rows(table_name="signal_preview_events", hours=hours)

    def _summarize_signal_rows(self, *, table_name: str, hours: int = 24) -> List[Tuple]:
        sql = (
            "SELECT symbol, timeframe, strategy, action, COUNT(*)::bigint AS count, "
            "AVG(confidence)::double precision AS avg_confidence, MAX(generated_at) AS last_seen_at "
            f"FROM {table_name} "
            "WHERE generated_at >= NOW() - (%s * INTERVAL '1 hour') "
            "GROUP BY symbol, timeframe, strategy, action "
            "ORDER BY symbol, timeframe, strategy, action"
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
                executed_at = _dt.fromisoformat(str(entry.get("at") or "")).replace(tzinfo=_tz.utc)
            except (ValueError, TypeError):
                executed_at = _dt.now(_tz.utc)
            batch.append(
                (
                    executed_at,
                    entry.get("signal_id") or "",
                    entry.get("symbol") or "",
                    entry.get("action") or "",
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
            metadata = row[12] if len(row) > 12 and row[12] is not None else {}
            batch.append((*row[:12], self._writer._json(metadata)))
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
