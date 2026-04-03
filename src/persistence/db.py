"""
TimescaleDB connection management and persistence facade.
DDL and concrete SQL statements live under ``src/persistence/schema``.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Iterable, List, Optional

import psycopg2
from psycopg2.extras import Json, execute_batch
from psycopg2.pool import SimpleConnectionPool

from src.config import DBSettings
from src.persistence.repositories import (
    EconomicCalendarRepository,
    MarketRepository,
    RuntimeStatusRepository,
    SignalEventRepository,
    TradingStateRepository,
    TradeCommandAuditRepository,
)
from src.persistence.schema import DDL_STATEMENTS

logger = logging.getLogger(__name__)


class TimescaleWriter:
    """TimescaleDB writer with connection pooling and repository-backed facade methods."""

    def __init__(self, settings: DBSettings, min_conn: int = 1, max_conn: int = 10):
        self.settings = settings
        self._pool: Optional[SimpleConnectionPool] = None
        self._min_conn = min_conn
        self._max_conn = max_conn
        self._last_health_check = 0
        self._health_check_interval = 60
        self._reconnect_lock = threading.Lock()
        self._market_repo: Optional[MarketRepository] = None
        self._signal_repo: Optional[SignalEventRepository] = None
        self._trade_command_repo: Optional[TradeCommandAuditRepository] = None
        self._trading_state_repo: Optional[TradingStateRepository] = None
        self._economic_repo: Optional[EconomicCalendarRepository] = None
        self._runtime_repo: Optional[RuntimeStatusRepository] = None
        self._init_pool()

    @property
    def market_repo(self) -> MarketRepository:
        repo = getattr(self, "_market_repo", None)
        if repo is None:
            repo = MarketRepository(self)
            self._market_repo = repo
        return repo

    @property
    def signal_repo(self) -> SignalEventRepository:
        repo = getattr(self, "_signal_repo", None)
        if repo is None:
            repo = SignalEventRepository(self)
            self._signal_repo = repo
        return repo

    @property
    def trade_command_repo(self) -> TradeCommandAuditRepository:
        repo = getattr(self, "_trade_command_repo", None)
        if repo is None:
            repo = TradeCommandAuditRepository(self)
            self._trade_command_repo = repo
        return repo

    @property
    def economic_repo(self) -> EconomicCalendarRepository:
        repo = getattr(self, "_economic_repo", None)
        if repo is None:
            repo = EconomicCalendarRepository(self)
            self._economic_repo = repo
        return repo

    @property
    def trading_state_repo(self) -> TradingStateRepository:
        repo = getattr(self, "_trading_state_repo", None)
        if repo is None:
            repo = TradingStateRepository(self)
            self._trading_state_repo = repo
        return repo

    @property
    def runtime_repo(self) -> RuntimeStatusRepository:
        repo = getattr(self, "_runtime_repo", None)
        if repo is None:
            repo = RuntimeStatusRepository(self)
            self._runtime_repo = repo
        return repo

    def _json(self, value):
        return Json(value)

    def _init_pool(self):
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
            logger.info(
                "Database connection pool initialized: %s-%s connections",
                self._min_conn,
                self._max_conn,
            )
        except Exception as exc:
            logger.error("Failed to initialize connection pool: %s", exc)
            raise

    def _check_pool_health(self):
        now = time.time()
        if now - self._last_health_check < self._health_check_interval:
            return

        self._last_health_check = now
        if not self._pool:
            logger.warning("Connection pool not initialized")
            return

        try:
            conn = self._pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    if cur.fetchone()[0] != 1:
                        raise psycopg2.Error("Health check failed")
                logger.debug("Database health check passed")
            finally:
                self._pool.putconn(conn)
        except Exception as exc:
            logger.error("Database health check failed: %s", exc)
            self._reconnect()

    def _reconnect(self):
        if not self._reconnect_lock.acquire(blocking=False):
            with self._reconnect_lock:
                return
        try:
            logger.warning("Attempting to reconnect to database...")
            old_pool = self._pool
            self._pool = None
            if old_pool:
                try:
                    old_pool.closeall()
                except Exception:
                    pass
            self._init_pool()
            logger.info("Database reconnection successful")
        except Exception as exc:
            logger.error("Database reconnection failed: %s", exc)
        finally:
            self._reconnect_lock.release()

    @contextmanager
    def connection(self):
        self._check_pool_health()
        if not self._pool:
            raise RuntimeError("Connection pool not initialized")

        conn = None
        broken = False
        try:
            conn = self._pool.getconn()
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.settings.pg_schema}"')
                cur.execute(f"SET search_path TO {self.settings.pg_schema}, public")
            yield conn
        except psycopg2.OperationalError as exc:
            logger.error("Database connection error: %s", exc)
            broken = True
            self._reconnect()
            raise
        except Exception as exc:
            logger.error("Unexpected error in connection context: %s", exc)
            raise
        finally:
            if conn is not None:
                pool = self._pool
                if broken or conn.closed:
                    try:
                        conn.close()
                    except Exception:
                        pass
                elif pool is not None:
                    pool.putconn(conn)

    def init_schema(self) -> None:
        # Migration: rename legacy 'action' column to 'direction' in existing tables
        migrate = """
DO $$
DECLARE
    _tbl text;
BEGIN
    FOREACH _tbl IN ARRAY ARRAY[
        'signal_events', 'signal_preview_events', 'signal_outcomes',
        'auto_executions', 'trade_outcomes',
        'backtest_trades', 'backtest_signal_evaluations'
    ] LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = _tbl AND column_name = 'action'
        ) AND NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = _tbl AND column_name = 'direction'
        ) THEN
            EXECUTE format('ALTER TABLE %I RENAME COLUMN action TO direction', _tbl);
            RAISE NOTICE 'Renamed action → direction on %', _tbl;
        END IF;
    END LOOP;
END $$;
"""
        ddl = "CREATE EXTENSION IF NOT EXISTS timescaledb;\n" + migrate + "\n".join(DDL_STATEMENTS)
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(ddl)
        logger.info("Timescale schema ensured")

    def write_ticks(self, rows, page_size: int = 1000) -> None:
        self.market_repo.write_ticks(rows, page_size=page_size)

    def write_quotes(self, rows, page_size: int = 1000) -> None:
        self.market_repo.write_quotes(rows, page_size=page_size)

    def write_ohlc(self, rows, upsert: bool = False, page_size: int = 1000) -> None:
        self.market_repo.write_ohlc(rows, upsert=upsert, page_size=page_size)

    def write_ohlc_intrabar(self, rows, page_size: int = 1000) -> None:
        self.market_repo.write_ohlc_intrabar(rows, page_size=page_size)

    def last_ohlc_time(self, symbol: str, timeframe: str):
        return self.market_repo.last_ohlc_time(symbol, timeframe)

    def last_tick_time(self, symbol: str):
        return self.market_repo.last_tick_time(symbol)

    def last_quote_time(self, symbol: str):
        return self.market_repo.last_quote_time(symbol)

    def fetch_ohlc(self, symbol: str, timeframe: str, start_time, limit: int):
        return self.market_repo.fetch_ohlc(symbol, timeframe, start_time, limit)

    def fetch_recent_ohlc(self, symbol: str, timeframe: str, limit: int):
        return self.market_repo.fetch_recent_ohlc(symbol, timeframe, limit)

    def fetch_ohlc_before(self, symbol: str, timeframe: str, end_time, limit: int):
        return self.market_repo.fetch_ohlc_before(symbol, timeframe, end_time, limit)

    def fetch_recent_ticks(self, symbol: str, limit: int):
        return self.market_repo.fetch_recent_ticks(symbol, limit)

    def fetch_ticks(self, symbol: str, start_time, end_time, limit: int):
        return self.market_repo.fetch_ticks(symbol, start_time, end_time, limit)

    def fetch_quotes(self, symbol: str, start_time, end_time, limit: int):
        return self.market_repo.fetch_quotes(symbol, start_time, end_time, limit)

    def fetch_ohlc_range(self, symbol: str, timeframe: str, start_time, end_time, limit: int):
        return self.market_repo.fetch_ohlc_range(symbol, timeframe, start_time, end_time, limit)

    def write_economic_calendar(self, rows, page_size: int = 500) -> None:
        self.economic_repo.write_economic_calendar(rows, page_size=page_size)

    def write_economic_calendar_updates(self, rows, page_size: int = 500) -> None:
        self.economic_repo.write_economic_calendar_updates(rows, page_size=page_size)

    def delete_economic_calendar_by_keys(self, keys) -> None:
        self.economic_repo.delete_economic_calendar_by_keys(keys)

    def fetch_economic_calendar(
        self,
        start_time,
        end_time,
        limit: int,
        sources=None,
        countries=None,
        currencies=None,
        session_buckets=None,
        statuses=None,
        importance_min=None,
    ):
        return self.economic_repo.fetch_economic_calendar(
            start_time,
            end_time,
            limit,
            sources=sources,
            countries=countries,
            currencies=currencies,
            session_buckets=session_buckets,
            statuses=statuses,
            importance_min=importance_min,
        )

    def fetch_economic_calendar_by_uids(self, event_uids):
        return self.economic_repo.fetch_economic_calendar_by_uids(event_uids)

    def fetch_economic_calendar_updates(
        self,
        start_time,
        end_time,
        limit: int,
        event_uid=None,
        snapshot_reasons=None,
        job_types=None,
    ):
        return self.economic_repo.fetch_economic_calendar_updates(
            start_time,
            end_time,
            limit,
            event_uid=event_uid,
            snapshot_reasons=snapshot_reasons,
            job_types=job_types,
        )

    def write_market_impact(self, rows, page_size: int = 100) -> None:
        self.economic_repo.write_market_impact(rows, page_size=page_size)

    def fetch_market_impact_by_event(self, event_uid, symbol):
        return self.economic_repo.fetch_market_impact_by_event(event_uid, symbol)

    def fetch_market_impact_stats(self, **kwargs):
        return self.economic_repo.fetch_market_impact_stats(**kwargs)

    def fetch_released_events_without_impact(self, symbols, since, importance_min=2, limit=500):
        return self.economic_repo.fetch_released_events_without_impact(
            symbols, since, importance_min=importance_min, limit=limit
        )

    def write_runtime_task_status(self, rows, page_size: int = 200) -> None:
        self.runtime_repo.write_runtime_task_status(rows, page_size=page_size)

    def fetch_runtime_task_status(self, component=None, task_name=None):
        return self.runtime_repo.fetch_runtime_task_status(component=component, task_name=task_name)

    def write_trade_command_audits(self, rows, page_size: int = 200) -> None:
        self.trade_command_repo.write_trade_command_audits(rows, page_size=page_size)

    def fetch_trade_command_audits(self, **kwargs):
        return self.trade_command_repo.fetch_trade_command_audits(**kwargs)

    def summarize_trade_command_audits(self, **kwargs):
        return self.trade_command_repo.summarize_trade_command_audits(**kwargs)

    def write_pending_order_states(self, rows, page_size: int = 200) -> None:
        self.trading_state_repo.write_pending_order_states(rows, page_size=page_size)

    def fetch_pending_order_states(self, **kwargs):
        return self.trading_state_repo.fetch_pending_order_states(**kwargs)

    def write_position_runtime_states(self, rows, page_size: int = 200) -> None:
        self.trading_state_repo.write_position_runtime_states(rows, page_size=page_size)

    def fetch_position_runtime_states(self, **kwargs):
        return self.trading_state_repo.fetch_position_runtime_states(**kwargs)

    def write_trade_control_states(self, rows, page_size: int = 50) -> None:
        self.trading_state_repo.write_trade_control_states(rows, page_size=page_size)

    def fetch_trade_control_state(self, *, account_alias: str):
        return self.trading_state_repo.fetch_trade_control_state(account_alias=account_alias)

    def write_signal_events(self, rows, page_size: int = 200) -> None:
        self.signal_repo.write_signal_events(rows, page_size=page_size)

    def write_signal_preview_events(self, rows, page_size: int = 200) -> None:
        self.signal_repo.write_signal_preview_events(rows, page_size=page_size)

    def fetch_signal_events(self, **kwargs):
        return self.signal_repo.fetch_signal_events(**kwargs)

    def fetch_signal_preview_events(self, **kwargs):
        return self.signal_repo.fetch_signal_preview_events(**kwargs)

    def summarize_signal_events(self, **kwargs):
        return self.signal_repo.summarize_signal_events(**kwargs)

    def summarize_signal_preview_events(self, **kwargs):
        return self.signal_repo.summarize_signal_preview_events(**kwargs)

    def write_outcome_events(self, rows, page_size: int = 200) -> None:
        self.signal_repo.write_outcome_events(rows, page_size=page_size)

    def write_auto_executions(self, rows, page_size: int = 200) -> None:
        self.signal_repo.write_auto_executions(rows, page_size=page_size)

    def fetch_winrates(self, **kwargs):
        return self.signal_repo.fetch_winrates(**kwargs)

    def write_trade_outcomes(self, rows, page_size: int = 200) -> None:
        self.signal_repo.write_trade_outcomes(rows, page_size=page_size)

    def fetch_expectancy_stats(self, **kwargs):
        return self.signal_repo.fetch_expectancy_stats(**kwargs)

    def get_pool_stats(self) -> dict:
        if not self._pool:
            return {"status": "pool_not_initialized"}

        try:
            return {
                "status": "healthy",
                "min_connections": self._min_conn,
                "max_connections": self._max_conn,
                "current_connections": getattr(self._pool, "_used", 0),
                "available_connections": getattr(self._pool, "_rused", 0),
                "last_health_check": self._last_health_check,
            }
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def close(self):
        if self._pool:
            self._pool.closeall()
            logger.info("Database connection pool closed")

    def _batch(self, sql: str, rows: Iterable[tuple], page_size: int = 1000) -> None:
        batch = list(rows)
        if not batch:
            return

        if len(batch) > 10000:
            page_size = min(5000, len(batch) // 10)
        elif len(batch) < 100:
            page_size = len(batch)

        for i in range(0, len(batch), page_size):
            chunk = batch[i:i + page_size]
            self._write_chunk_with_retry(sql, chunk, page_size)

        logger.debug("Inserted %s rows", len(batch))

    def _write_chunk_with_retry(
        self,
        sql: str,
        chunk: List[tuple],
        page_size: int,
        max_retries: int = 3,
    ):
        for attempt in range(max_retries):
            try:
                with self.connection() as conn, conn.cursor() as cur:
                    execute_batch(cur, sql, chunk, page_size=min(page_size, len(chunk)))
                logger.debug("Successfully inserted %s rows (attempt %s)", len(chunk), attempt + 1)
                return
            except psycopg2.OperationalError as exc:
                if attempt == max_retries - 1:
                    logger.error(
                        "Failed to insert %s rows after %s attempts: %s",
                        len(chunk),
                        max_retries,
                        exc,
                    )
                    raise

                wait_time = 2 ** attempt
                logger.warning(
                    "Insert failed (attempt %s), retrying in %s seconds: %s",
                    attempt + 1,
                    wait_time,
                    exc,
                )
                time.sleep(wait_time)
                if "connection" in str(exc).lower():
                    self._reconnect()
            except Exception as exc:
                logger.error("Unexpected error inserting %s rows: %s", len(chunk), exc)
                if attempt == max_retries - 1:
                    raise
                time.sleep(2 ** attempt)
