"""
TimescaleDB 连接与基础批量写入。
DDL 和具体 SQL 语句在 src/persistence/schema 中集中管理。
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Iterable, Tuple, Optional, List

import psycopg2
from psycopg2.extras import Json, execute_batch, execute_values
from psycopg2.pool import SimpleConnectionPool

from src.config import DBSettings, load_db_settings
from src.persistence.schema import (
    DELETE_ECONOMIC_CALENDAR_BY_KEYS_SQL,
    DDL_STATEMENTS,
    INSERT_TICKS_SQL,
    INSERT_QUOTES_SQL,
    INSERT_OHLC_SQL,
    UPSERT_OHLC_SQL,
    INSERT_INTRABAR_SQL,
    INSERT_ECONOMIC_CALENDAR_UPDATE_SQL,
    INSERT_TRADE_OPERATIONS_SQL,
    INSERT_SIGNAL_EVENTS_SQL,
    INSERT_SIGNAL_PREVIEW_EVENTS_SQL,
    INSERT_SIGNAL_OUTCOMES_SQL,
    SIGNAL_OUTCOMES_WINRATE_SQL,
    INSERT_AUTO_EXECUTIONS_SQL,
    UPSERT_RUNTIME_TASK_STATUS_SQL,
    UPSERT_ECONOMIC_CALENDAR_SQL,
)
from src.persistence.validator import DataValidator

logger = logging.getLogger(__name__)
TICK_ORDER_SQL = "COALESCE(time_msc, FLOOR(EXTRACT(EPOCH FROM time) * 1000)::bigint)"


class TimescaleWriter:
    """TimescaleDB writer with connection pooling and enhanced error handling."""

    def __init__(self, settings: DBSettings, min_conn: int = 1, max_conn: int = 10):
        self.settings = settings
        self._pool: Optional[SimpleConnectionPool] = None
        self._min_conn = min_conn
        self._max_conn = max_conn
        self._last_health_check = 0
        self._health_check_interval = 60  # seconds
        self._init_pool()

    def _init_pool(self):
        """初始化连接池"""
        try:
            self._pool = SimpleConnectionPool(
                self._min_conn,
                self._max_conn,
                host=self.settings.pg_host,
                port=self.settings.pg_port,
                user=self.settings.pg_user,
                password=self.settings.pg_password,
                dbname=self.settings.pg_database,
                connect_timeout=10,
            )
            logger.info(f"Database connection pool initialized: {self._min_conn}-{self._max_conn} connections")
        except Exception as e:
            logger.error(f"Failed to initialize connection pool: {e}")
            raise

    def _check_pool_health(self):
        """检查连接池健康状态"""
        now = time.time()
        if now - self._last_health_check < self._health_check_interval:
            return
        
        self._last_health_check = now
        
        if not self._pool:
            logger.warning("Connection pool not initialized")
            return
        
        try:
            # 尝试获取一个连接来测试
            conn = self._pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    if cur.fetchone()[0] != 1:
                        raise psycopg2.Error("Health check failed")
                logger.debug("Database health check passed")
            finally:
                self._pool.putconn(conn)
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
            # 尝试重新初始化连接池
            self._reconnect()

    def _reconnect(self):
        """重新连接数据库"""
        logger.warning("Attempting to reconnect to database...")
        try:
            if self._pool:
                self._pool.closeall()
            self._init_pool()
            logger.info("Database reconnection successful")
        except Exception as e:
            logger.error(f"Database reconnection failed: {e}")

    @contextmanager
    def connection(self):
        """获取数据库连接（使用连接池）"""
        self._check_pool_health()
        
        if not self._pool:
            raise RuntimeError("Connection pool not initialized")
        
        conn = None
        try:
            conn = self._pool.getconn()
            conn.autocommit = True
            
            # 确保 schema 存在并设置 search_path
            with conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.settings.pg_schema}"')
                cur.execute(f"SET search_path TO {self.settings.pg_schema}, public")
            
            yield conn
        except psycopg2.OperationalError as e:
            logger.error(f"Database connection error: {e}")
            if conn:
                try:
                    conn.close()
                except:
                    pass
            self._reconnect()
            raise
        except Exception as e:
            logger.error(f"Unexpected error in connection context: {e}")
            raise
        finally:
            if conn and not conn.closed:
                self._pool.putconn(conn)

    def init_schema(self) -> None:
        # 幂等创建 hypertable，未安装 timescaledb 扩展会失败需手动安装。
        ddl = "CREATE EXTENSION IF NOT EXISTS timescaledb;\n" + "\n".join(DDL_STATEMENTS)
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(ddl)
        logger.info("Timescale schema ensured")

    def write_ticks(self, rows: Iterable[Tuple], page_size: int = 1000) -> None:
        """
        写入 tick 数据，自动验证数据有效性
        
        Args:
            rows: (symbol, price, volume, iso_time[, time_msc])
            page_size: 批量写入大小
        """
        rows_list = list(rows)
        if not rows_list:
            return
        
        # 验证数据有效性
        valid_rows = DataValidator.filter_valid_ticks(rows_list)
        
        if not valid_rows:
            logger.warning("No valid tick data to write")
            return
        
        # 记录验证结果
        invalid_count = len(rows_list) - len(valid_rows)
        if invalid_count > 0:
            logger.warning(f"Dropped {invalid_count} invalid tick rows")
        
        normalized = []
        for row in valid_rows:
            if len(row) >= 5:
                normalized.append((row[0], row[1], row[2], row[3], row[4]))
                continue

            time_value = datetime.fromisoformat(str(row[3]).replace("Z", "+00:00"))
            normalized.append((row[0], row[1], row[2], row[3], int(time_value.timestamp() * 1000)))

        self._batch(INSERT_TICKS_SQL, normalized, page_size=page_size)

    def write_quotes(self, rows: Iterable[Tuple[str, float, float, float, float, str]], page_size: int = 1000) -> None:
        """
        写入报价数据，自动验证数据有效性
        
        Args:
            rows: (symbol, bid, ask, last, volume, iso_time)
            page_size: 批量写入大小
        """
        rows_list = list(rows)
        if not rows_list:
            return
        
        # 验证数据有效性
        valid_rows = DataValidator.filter_valid_quotes(rows_list)
        
        if not valid_rows:
            logger.warning("No valid quote data to write")
            return
        
        # 记录验证结果
        invalid_count = len(rows_list) - len(valid_rows)
        if invalid_count > 0:
            logger.warning(f"Dropped {invalid_count} invalid quote rows")
        
        self._batch(INSERT_QUOTES_SQL, valid_rows, page_size=page_size)

    def write_ohlc(self, rows: Iterable[Tuple], upsert: bool = False, page_size: int = 1000) -> None:
        """
        写入 OHLC 数据，自动验证数据有效性
        
        Args:
            rows: (symbol, timeframe, open, high, low, close, volume, iso_time[, indicators])
            upsert: 是否使用 UPSERT
            page_size: 批量写入大小
        """
        rows_list = list(rows)
        if not rows_list:
            return
        
        # 验证数据有效性
        valid_rows = DataValidator.filter_valid_ohlc(rows_list, upsert)
        
        if not valid_rows:
            logger.warning("No valid OHLC data to write")
            return
        
        # 记录验证结果
        invalid_count = len(rows_list) - len(valid_rows)
        if invalid_count > 0:
            logger.warning(f"Dropped {invalid_count} invalid OHLC rows")
        
        # 标准化数据格式
        normalized = []
        for row in valid_rows:
            if len(row) == 8:
                normalized.append((*row, Json({})))
            else:
                indicators = row[8] if row[8] is not None else {}
                normalized.append((*row[:8], Json(indicators)))
        
        sql = UPSERT_OHLC_SQL if upsert else INSERT_OHLC_SQL
        self._batch(sql, normalized, page_size=page_size)

    def write_ohlc_intrabar(self, rows: Iterable[Tuple[str, str, float, float, float, float, float, str, str]], page_size: int = 1000) -> None:
        """
        写入盘中数据，自动验证数据有效性
        
        Args:
            rows: (symbol, timeframe, open, high, low, close, volume, bar_time, recorded_at)
            page_size: 批量写入大小
        """
        rows_list = list(rows)
        if not rows_list:
            return
        
        # 验证数据有效性
        valid_rows = DataValidator.filter_valid_intrabar(rows_list)
        
        if not valid_rows:
            logger.warning("No valid intrabar data to write")
            return
        
        # 记录验证结果
        invalid_count = len(rows_list) - len(valid_rows)
        if invalid_count > 0:
            logger.warning(f"Dropped {invalid_count} invalid intrabar rows")
        
        self._batch(INSERT_INTRABAR_SQL, valid_rows, page_size=page_size)

    def last_ohlc_time(self, symbol: str, timeframe: str) -> Optional[datetime]:
        sql = "SELECT max(time) FROM ohlc WHERE symbol=%s AND timeframe=%s"
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (symbol, timeframe))
            row = cur.fetchone()
            return row[0] if row and row[0] else None

    def last_tick_time(self, symbol: str) -> Optional[datetime]:
        sql = (
            "SELECT time FROM ticks WHERE symbol=%s "
            f"ORDER BY {TICK_ORDER_SQL} DESC, time DESC LIMIT 1"
        )
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (symbol,))
            row = cur.fetchone()
            return row[0] if row and row[0] else None

    def last_quote_time(self, symbol: str) -> Optional[datetime]:
        sql = "SELECT max(time) FROM quotes WHERE symbol=%s"
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (symbol,))
            row = cur.fetchone()
            return row[0] if row and row[0] else None

    def fetch_ohlc(
        self,
        symbol: str,
        timeframe: str,
        start_time: Optional[datetime],
        limit: int,
    ) -> List[Tuple]:
        sql = (
            "SELECT symbol, timeframe, open, high, low, close, volume, time, indicators "
            "FROM ohlc WHERE symbol=%s AND timeframe=%s"
        )
        params: List = [symbol, timeframe]
        if start_time:
            sql += " AND time > %s"
            params.append(start_time)
        sql += " ORDER BY time ASC LIMIT %s"
        params.append(limit)
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def fetch_recent_ohlc(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
    ) -> List[Tuple]:
        sql = (
            "SELECT symbol, timeframe, open, high, low, close, volume, time, indicators "
            "FROM ("
            "    SELECT symbol, timeframe, open, high, low, close, volume, time, indicators "
            "    FROM ohlc WHERE symbol=%s AND timeframe=%s "
            "    ORDER BY time DESC LIMIT %s"
            ") recent "
            "ORDER BY time ASC"
        )
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (symbol, timeframe, limit))
            return cur.fetchall()

    def fetch_ohlc_before(
        self,
        symbol: str,
        timeframe: str,
        end_time: datetime,
        limit: int,
    ) -> List[Tuple]:
        sql = (
            "SELECT symbol, timeframe, open, high, low, close, volume, time, indicators "
            "FROM ("
            "    SELECT symbol, timeframe, open, high, low, close, volume, time, indicators "
            "    FROM ohlc WHERE symbol=%s AND timeframe=%s AND time <= %s "
            "    ORDER BY time DESC LIMIT %s"
            ") recent "
            "ORDER BY time ASC"
        )
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (symbol, timeframe, end_time, limit))
            return cur.fetchall()

    def fetch_recent_ticks(
        self,
        symbol: str,
        limit: int,
    ) -> List[Tuple[str, float, float, datetime, Optional[int]]]:
        sql = (
            "SELECT symbol, price, volume, time, time_msc "
            "FROM ("
            "    SELECT symbol, price, volume, time, time_msc "
            "    FROM ticks WHERE symbol=%s "
            f"    ORDER BY {TICK_ORDER_SQL} DESC, time DESC LIMIT %s"
            ") recent "
            f"ORDER BY {TICK_ORDER_SQL} ASC, time ASC"
        )
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (symbol, limit))
            return cur.fetchall()

    def fetch_ticks(
        self,
        symbol: str,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: int,
    ) -> List[Tuple[str, float, float, datetime, Optional[int]]]:
        sql = "SELECT symbol, price, volume, time, time_msc FROM ticks WHERE symbol=%s"
        params: List = [symbol]
        if start_time is not None:
            sql += " AND time >= %s"
            params.append(start_time)
        if end_time is not None:
            sql += " AND time <= %s"
            params.append(end_time)
        sql += f" ORDER BY {TICK_ORDER_SQL} ASC, time ASC LIMIT %s"
        params.append(limit)
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def fetch_quotes(
        self,
        symbol: str,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: int,
    ) -> List[Tuple[str, float, float, float, float, datetime]]:
        sql = "SELECT symbol, bid, ask, last, volume, time FROM quotes WHERE symbol=%s"
        params: List = [symbol]
        if start_time is not None:
            sql += " AND time >= %s"
            params.append(start_time)
        if end_time is not None:
            sql += " AND time <= %s"
            params.append(end_time)
        sql += " ORDER BY time ASC LIMIT %s"
        params.append(limit)
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def fetch_ohlc_range(
        self,
        symbol: str,
        timeframe: str,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: int,
    ) -> List[Tuple]:
        sql = (
            "SELECT symbol, timeframe, open, high, low, close, volume, time, indicators "
            "FROM ohlc WHERE symbol=%s AND timeframe=%s"
        )
        params: List = [symbol, timeframe]
        if start_time is not None:
            sql += " AND time >= %s"
            params.append(start_time)
        if end_time is not None:
            sql += " AND time <= %s"
            params.append(end_time)
        sql += " ORDER BY time ASC LIMIT %s"
        params.append(limit)
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def write_economic_calendar(self, rows: Iterable[Tuple], page_size: int = 500) -> None:
        batch = []
        for row in rows:
            payload = row[31] if row[31] is not None else {}
            batch.append((*row[:31], Json(payload), row[32], row[33]))
        if not batch:
            return
        self._batch(UPSERT_ECONOMIC_CALENDAR_SQL, batch, page_size=page_size)

    def write_economic_calendar_updates(self, rows: Iterable[Tuple], page_size: int = 500) -> None:
        batch = []
        for row in rows:
            payload = row[16] if row[16] is not None else {}
            batch.append((*row[:16], Json(payload)))
        if not batch:
            return
        self._batch(INSERT_ECONOMIC_CALENDAR_UPDATE_SQL, batch, page_size=page_size)

    def delete_economic_calendar_by_keys(self, keys: Iterable[Tuple[datetime, str]]) -> None:
        doomed = list(keys)
        if not doomed:
            return
        with self.connection() as conn, conn.cursor() as cur:
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
        with self.connection() as conn, conn.cursor() as cur:
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
        with self.connection() as conn, conn.cursor() as cur:
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
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def write_runtime_task_status(self, rows: Iterable[Tuple], page_size: int = 200) -> None:
        batch = []
        for row in rows:
            details = row[12] if row[12] is not None else {}
            batch.append((*row[:12], Json(details)))
        if not batch:
            return
        self._batch(UPSERT_RUNTIME_TASK_STATUS_SQL, batch, page_size=page_size)

    def fetch_runtime_task_status(
        self,
        component: Optional[str] = None,
        task_name: Optional[str] = None,
    ) -> List[Tuple]:
        sql = (
            "SELECT component, task_name, updated_at, state, started_at, completed_at, "
            "next_run_at, duration_ms, success_count, failure_count, consecutive_failures, "
            "last_error, details "
            "FROM runtime_task_status WHERE 1=1"
        )
        params: List = []
        if component is not None:
            sql += " AND component = %s"
            params.append(component)
        if task_name is not None:
            sql += " AND task_name = %s"
            params.append(task_name)
        sql += " ORDER BY component ASC, task_name ASC"
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def write_trade_operations(self, rows: Iterable[Tuple], page_size: int = 200) -> None:
        batch = []
        for row in rows:
            request_payload = row[15] if row[15] is not None else {}
            response_payload = row[16] if row[16] is not None else {}
            batch.append((*row[:15], Json(request_payload), Json(response_payload)))
        if not batch:
            return
        self._batch(INSERT_TRADE_OPERATIONS_SQL, batch, page_size=page_size)

    def fetch_trade_operations(
        self,
        *,
        account_alias: Optional[str] = None,
        operation_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Tuple]:
        sql = (
            "SELECT recorded_at, operation_id, account_alias, operation_type, status, "
            "symbol, side, order_kind, volume, ticket, order_id, deal_id, magic, "
            "duration_ms, error_message, request_payload, response_payload "
            "FROM trade_operations WHERE 1=1"
        )
        params: List = []
        if account_alias is not None:
            sql += " AND account_alias = %s"
            params.append(account_alias)
        if operation_type is not None:
            sql += " AND operation_type = %s"
            params.append(operation_type)
        if status is not None:
            sql += " AND status = %s"
            params.append(status)
        sql += " ORDER BY recorded_at DESC LIMIT %s"
        params.append(limit)
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def summarize_trade_operations(
        self,
        *,
        hours: int = 24,
        account_alias: Optional[str] = None,
    ) -> List[Tuple]:
        sql = (
            "SELECT account_alias, operation_type, status, COUNT(*) AS count, "
            "AVG(duration_ms)::double precision AS avg_duration_ms, MAX(recorded_at) AS last_seen_at "
            "FROM trade_operations "
            "WHERE recorded_at >= NOW() - (%s * INTERVAL '1 hour')"
        )
        params: List = [max(1, int(hours))]
        if account_alias is not None:
            sql += " AND account_alias = %s"
            params.append(account_alias)
        sql += " GROUP BY account_alias, operation_type, status ORDER BY account_alias, operation_type, status"
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    


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
            batch.append((*row[:8], Json(used_indicators), Json(indicators_snapshot), Json(metadata)))
        if not batch:
            return
        self._batch(sql, batch, page_size=page_size)

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
        with self.connection() as conn, conn.cursor() as cur:
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
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, [max(1, int(hours))])
            return cur.fetchall()

    def write_outcome_events(self, rows: Iterable[Tuple], page_size: int = 200) -> None:
        """批量写入信号结果记录（胜负标记）。

        行格式：(recorded_at, signal_id, symbol, timeframe, strategy, action,
                  confidence, entry_price, exit_price, price_change, won, bars_held,
                  regime, metadata_dict)
        """
        batch = []
        for row in rows:
            metadata = row[13] if len(row) > 13 and row[13] is not None else {}
            batch.append((*row[:13], Json(metadata)))
        if not batch:
            return
        self._batch(INSERT_SIGNAL_OUTCOMES_SQL, batch, page_size=page_size)

    def write_auto_executions(self, rows: Iterable[dict], page_size: int = 200) -> None:
        """T-4: 批量写入自动交易执行记录。

        ``rows`` 为 ``TradeExecutor._execute`` / ``_handle_confirmed`` 构造的
        log_entry dict 列表，包含以下字段（缺失字段用 None 填充）：

        .. code-block:: python

            {
                "at": "2024-01-01T00:00:00+00:00",
                "signal_id": "...",
                "symbol": "XAUUSD",
                "action": "buy",
                "strategy": "sma_trend",
                "confidence": 0.75,
                "params": {"volume": 0.01, "sl": 1900.0, "tp": 1950.0, "rr": 2.0},
                "success": True,
                "error": None,
            }
        """
        from datetime import datetime as _dt, timezone as _tz

        batch = []
        for entry in rows:
            params = entry.get("params") or {}
            try:
                executed_at = _dt.fromisoformat(str(entry.get("at") or "")).replace(
                    tzinfo=_tz.utc
                )
            except (ValueError, TypeError):
                executed_at = _dt.now(_tz.utc)
            batch.append((
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
                Json({}),
            ))
        if not batch:
            return
        self._batch(INSERT_AUTO_EXECUTIONS_SQL, batch, page_size=page_size)

    def fetch_winrates(
        self,
        *,
        hours: int = 168,
        symbol: Optional[str] = None,
    ) -> List[Tuple]:
        """查询各策略胜率汇总。

        返回列：(strategy, action, total, wins, win_rate, avg_confidence, avg_move)
        """
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(SIGNAL_OUTCOMES_WINRATE_SQL, [max(1, hours), symbol, symbol])
            return cur.fetchall()

    def get_pool_stats(self) -> dict:
        """获取连接池统计信息"""
        if not self._pool:
            return {"status": "pool_not_initialized"}
        
        try:
            return {
                "status": "healthy",
                "min_connections": self._min_conn,
                "max_connections": self._max_conn,
                "current_connections": getattr(self._pool, '_used', 0),
                "available_connections": getattr(self._pool, '_rused', 0),
                "last_health_check": self._last_health_check,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}
    
    def close(self):
        """关闭连接池"""
        if self._pool:
            self._pool.closeall()
            logger.info("Database connection pool closed")

    def _batch(self, sql: str, rows: Iterable[tuple], page_size: int = 1000) -> None:
        """批量写入数据，支持重试和自适应批量大小"""
        batch = list(rows)
        if not batch:
            return
        
        # 自适应调整 page_size
        if len(batch) > 10000:
            page_size = min(5000, len(batch) // 10)
        elif len(batch) < 100:
            page_size = len(batch)
        
        # 分批写入，每批单独重试
        for i in range(0, len(batch), page_size):
            chunk = batch[i:i + page_size]
            self._write_chunk_with_retry(sql, chunk, page_size)
        
        logger.debug("Inserted %s rows", len(batch))
    
    def _write_chunk_with_retry(self, sql: str, chunk: List[tuple], page_size: int, max_retries: int = 3):
        """写入数据块，支持重试机制"""
        for attempt in range(max_retries):
            try:
                with self.connection() as conn, conn.cursor() as cur:
                    execute_batch(cur, sql, chunk, page_size=min(page_size, len(chunk)))
                logger.debug("Successfully inserted %s rows (attempt %s)", len(chunk), attempt + 1)
                return
            except psycopg2.OperationalError as e:
                if attempt == max_retries - 1:
                    logger.error("Failed to insert %s rows after %s attempts: %s", 
                                len(chunk), max_retries, e)
                    raise
                
                # 指数退避
                wait_time = 2 ** attempt
                logger.warning("Insert failed (attempt %s), retrying in %s seconds: %s", 
                             attempt + 1, wait_time, e)
                time.sleep(wait_time)
                
                # 如果是连接错误，尝试重连
                if "connection" in str(e).lower():
                    self._reconnect()
            except Exception as e:
                logger.error("Unexpected error inserting %s rows: %s", len(chunk), e)
                if attempt == max_retries - 1:
                    raise
                time.sleep(2 ** attempt)
