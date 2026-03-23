from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Iterable, List, Optional, Tuple

from psycopg2.extras import execute_values

from src.persistence.schema import (
    INSERT_ECONOMIC_CALENDAR_UPDATE_SQL,
    MARKET_IMPACT_AGGREGATED_STATS_SQL,
    UPSERT_ECONOMIC_CALENDAR_SQL,
    UPSERT_MARKET_IMPACT_SQL,
)

if TYPE_CHECKING:
    from src.persistence.db import TimescaleWriter


class EconomicCalendarRepository:
    def __init__(self, writer: "TimescaleWriter"):
        self._writer = writer

    def write_economic_calendar(self, rows: Iterable[Tuple], page_size: int = 500) -> None:
        batch = []
        for row in rows:
            payload = row[31] if row[31] is not None else {}
            batch.append((*row[:31], self._writer._json(payload), row[32], row[33]))
        if not batch:
            return
        self._writer._batch(UPSERT_ECONOMIC_CALENDAR_SQL, batch, page_size=page_size)

    def write_economic_calendar_updates(self, rows: Iterable[Tuple], page_size: int = 500) -> None:
        batch = []
        for row in rows:
            payload = row[16] if row[16] is not None else {}
            batch.append((*row[:16], self._writer._json(payload)))
        if not batch:
            return
        self._writer._batch(INSERT_ECONOMIC_CALENDAR_UPDATE_SQL, batch, page_size=page_size)

    def delete_economic_calendar_by_keys(self, keys: Iterable[Tuple[datetime, str]]) -> None:
        doomed = list(keys)
        if not doomed:
            return
        with self._writer.connection() as conn, conn.cursor() as cur:
            execute_values(
                cur,
                (
                    "DELETE FROM economic_calendar_events target "
                    "USING (VALUES %s) AS doomed(scheduled_at, event_uid) "
                    "WHERE target.scheduled_at = doomed.scheduled_at "
                    "AND target.event_uid = doomed.event_uid"
                ),
                doomed,
                page_size=min(1000, len(doomed)),
            )

    def fetch_economic_calendar(
        self,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: int,
        sources: Optional[List[str]] = None,
        countries: Optional[List[str]] = None,
        currencies: Optional[List[str]] = None,
        session_buckets: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        importance_min: Optional[int] = None,
    ) -> List[Tuple]:
        sql = (
            "SELECT scheduled_at, event_uid, source, provider_event_id, event_name, "
            "country, category, currency, reference, actual, previous, forecast, "
            "revised, importance, unit, release_id, source_url, all_day, "
            "scheduled_at_local, local_timezone, scheduled_at_release, release_timezone, "
            "session_bucket, is_asia_session, is_europe_session, is_us_session, "
            "status, first_seen_at, last_seen_at, released_at, last_value_check_at, "
            "raw_payload, ingested_at, last_updated "
            "FROM economic_calendar_events WHERE 1=1"
        )
        params: List = []
        if start_time is not None:
            sql += " AND scheduled_at >= %s"
            params.append(start_time)
        if end_time is not None:
            sql += " AND scheduled_at <= %s"
            params.append(end_time)
        if sources:
            sql += " AND source = ANY(%s)"
            params.append(sources)
        if countries:
            sql += " AND country = ANY(%s)"
            params.append(countries)
        if currencies:
            sql += " AND currency = ANY(%s)"
            params.append(currencies)
        if session_buckets:
            sql += " AND session_bucket = ANY(%s)"
            params.append(session_buckets)
        if statuses:
            sql += " AND status = ANY(%s)"
            params.append(statuses)
        if importance_min is not None:
            sql += " AND COALESCE(importance, 0) >= %s"
            params.append(importance_min)
        sql += " ORDER BY scheduled_at ASC LIMIT %s"
        params.append(limit)
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def fetch_economic_calendar_by_uids(self, event_uids: List[str]) -> List[Tuple]:
        if not event_uids:
            return []
        sql = (
            "SELECT DISTINCT ON (event_uid) "
            "scheduled_at, event_uid, source, provider_event_id, event_name, "
            "country, category, currency, reference, actual, previous, forecast, "
            "revised, importance, unit, release_id, source_url, all_day, "
            "scheduled_at_local, local_timezone, scheduled_at_release, release_timezone, "
            "session_bucket, is_asia_session, is_europe_session, is_us_session, "
            "status, first_seen_at, last_seen_at, released_at, last_value_check_at, "
            "raw_payload, ingested_at, last_updated "
            "FROM economic_calendar_events "
            "WHERE event_uid = ANY(%s) "
            "ORDER BY event_uid, last_updated DESC"
        )
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (event_uids,))
            return cur.fetchall()

    def fetch_economic_calendar_updates(
        self,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: int,
        event_uid: Optional[str] = None,
        snapshot_reasons: Optional[List[str]] = None,
        job_types: Optional[List[str]] = None,
    ) -> List[Tuple]:
        sql = (
            "SELECT recorded_at, event_uid, scheduled_at, source, provider_event_id, event_name, "
            "country, currency, status, snapshot_reason, job_type, actual, previous, forecast, "
            "revised, importance, raw_payload "
            "FROM economic_calendar_event_updates WHERE 1=1"
        )
        params: List = []
        if start_time is not None:
            sql += " AND recorded_at >= %s"
            params.append(start_time)
        if end_time is not None:
            sql += " AND recorded_at <= %s"
            params.append(end_time)
        if event_uid:
            sql += " AND event_uid = %s"
            params.append(event_uid)
        if snapshot_reasons:
            sql += " AND snapshot_reason = ANY(%s)"
            params.append(snapshot_reasons)
        if job_types:
            sql += " AND job_type = ANY(%s)"
            params.append(job_types)
        sql += " ORDER BY recorded_at DESC LIMIT %s"
        params.append(limit)
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    # ── Market Impact 行情影响统计 ────────────────────────────────────

    def write_market_impact(self, rows: Iterable[Tuple], page_size: int = 100) -> None:
        batch = list(rows)
        if not batch:
            return
        # metadata (jsonb) 是最后一个参数，需要序列化
        serialized = []
        for row in batch:
            meta = row[-1] if row[-1] is not None else {}
            serialized.append((*row[:-1], self._writer._json(meta)))
        self._writer._batch(UPSERT_MARKET_IMPACT_SQL, serialized, page_size=page_size)

    def fetch_market_impact_by_event(
        self, event_uid: str, symbol: str
    ) -> Optional[Tuple]:
        sql = (
            "SELECT * FROM economic_event_market_impact "
            "WHERE event_uid = %s AND symbol = %s "
            "ORDER BY recorded_at DESC LIMIT 1"
        )
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (event_uid, symbol))
            return cur.fetchone()

    def fetch_market_impact_stats(
        self,
        symbol: str = "XAUUSD",
        timeframe: str = "M5",
        event_name: Optional[str] = None,
        country: Optional[str] = None,
        importance_min: Optional[int] = None,
        limit: int = 50,
    ) -> List[Tuple]:
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(
                MARKET_IMPACT_AGGREGATED_STATS_SQL,
                (
                    symbol,
                    timeframe,
                    event_name,
                    event_name,
                    country,
                    country,
                    importance_min,
                    importance_min,
                    limit,
                ),
            )
            return cur.fetchall()

    def fetch_released_events_without_impact(
        self,
        symbols: List[str],
        since: datetime,
        importance_min: int = 2,
        limit: int = 500,
    ) -> List[Tuple]:
        """查找已发布但尚未统计行情影响的事件（用于 backfill）。"""
        sql = (
            "SELECT e.scheduled_at, e.event_uid, e.source, e.provider_event_id, e.event_name, "
            "e.country, e.category, e.currency, e.reference, e.actual, e.previous, e.forecast, "
            "e.revised, e.importance, e.unit, e.release_id, e.source_url, e.all_day, "
            "e.scheduled_at_local, e.local_timezone, e.scheduled_at_release, e.release_timezone, "
            "e.session_bucket, e.is_asia_session, e.is_europe_session, e.is_us_session, "
            "e.status, e.first_seen_at, e.last_seen_at, e.released_at, e.last_value_check_at, "
            "e.raw_payload, e.ingested_at, e.last_updated "
            "FROM economic_calendar_events e "
            "LEFT JOIN economic_event_market_impact m "
            "  ON e.event_uid = m.event_uid AND m.symbol = ANY(%s) "
            "WHERE e.status = 'released' "
            "  AND e.scheduled_at >= %s "
            "  AND COALESCE(e.importance, 0) >= %s "
            "  AND m.event_uid IS NULL "
            "ORDER BY e.scheduled_at ASC "
            "LIMIT %s"
        )
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (symbols, since, importance_min, limit))
            return cur.fetchall()
