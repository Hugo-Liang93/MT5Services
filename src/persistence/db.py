"""
TimescaleDB 连接与基础批量写入。
DDL 和具体 SQL 语句在 src/persistence/schema 中集中管理。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Iterable, Tuple, Optional

import psycopg2
from psycopg2.extras import execute_batch

from src.config import DBSettings, load_db_settings
from src.persistence.schema import (
    DDL_STATEMENTS,
    INSERT_TICKS_SQL,
    INSERT_QUOTES_SQL,
    INSERT_OHLC_SQL,
    UPSERT_OHLC_SQL,
    INSERT_INTRABAR_SQL,
    UPSERT_INDICATORS_SQL,
)

logger = logging.getLogger(__name__)


class TimescaleWriter:
    """Minimal TimescaleDB writer，专注连接和批量写入。"""

    def __init__(self, settings: DBSettings):
        self.settings = settings
        self._conn = None

    @contextmanager
    def connection(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(
                host=self.settings.pg_host,
                port=self.settings.pg_port,
                user=self.settings.pg_user,
                password=self.settings.pg_password,
                dbname=self.settings.pg_database,
            )
            self._conn.autocommit = True
            # 确保 schema 存在并设置 search_path（包含业务 schema + public）
            with self._conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.settings.pg_schema}"')
                cur.execute(f"SET search_path TO {self.settings.pg_schema}, public")
        try:
            yield self._conn
        except Exception:
            if self._conn:
                self._conn.close()
                self._conn = None
            raise

    def init_schema(self) -> None:
        # 幂等创建 hypertable，未安装 timescaledb 扩展会失败需手动安装。
        ddl = "CREATE EXTENSION IF NOT EXISTS timescaledb;\n" + "\n".join(DDL_STATEMENTS)
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(ddl)
        logger.info("Timescale schema ensured")

    def write_ticks(self, rows: Iterable[Tuple[str, float, float, str]], page_size: int = 1000) -> None:
        # rows: (symbol, price, volume, iso_time)
        self._batch(INSERT_TICKS_SQL, rows, page_size=page_size)

    def write_quotes(self, rows: Iterable[Tuple[str, float, float, float, float, str]], page_size: int = 1000) -> None:
        # rows: (symbol, bid, ask, last, volume, iso_time)
        self._batch(INSERT_QUOTES_SQL, rows, page_size=page_size)

    def write_ohlc(self, rows: Iterable[Tuple], upsert: bool = False, page_size: int = 1000) -> None:
        """
        rows: (symbol, timeframe, open, high, low, close, volume, iso_time[, indicators])
        支持末尾可选 indicators json。
        """
        rows_list = list(rows)
        if not rows_list:
            return
        if len(rows_list[0]) == 8:
            rows_list = [(*row, None) for row in rows_list]
        sql = UPSERT_OHLC_SQL if upsert else INSERT_OHLC_SQL
        self._batch(sql, rows_list, page_size=page_size)

    def write_ohlc_intrabar(self, rows: Iterable[Tuple[str, str, float, float, float, float, float, str, str]], page_size: int = 1000) -> None:
        # rows: (symbol, timeframe, open, high, low, close, volume, bar_time, recorded_at)
        self._batch(INSERT_INTRABAR_SQL, rows, page_size=page_size)

    def write_indicators(self, rows: Iterable[Tuple[str, str, str, float, str, str]], page_size: int = 1000) -> None:
        # rows: (symbol, timeframe, indicator, value, bar_time_iso, computed_at_iso)
        self._batch(UPSERT_INDICATORS_SQL, rows, page_size=page_size)

    def last_ohlc_time(self, symbol: str, timeframe: str) -> Optional[datetime]:
        sql = "SELECT max(time) FROM ohlc WHERE symbol=%s AND timeframe=%s"
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (symbol, timeframe))
            row = cur.fetchone()
            return row[0] if row and row[0] else None

    def _batch(self, sql: str, rows: Iterable[tuple], page_size: int = 1000) -> None:
        batch = list(rows)
        if not batch:
            return
        with self.connection() as conn, conn.cursor() as cur:
            execute_batch(cur, sql, batch, page_size=page_size)
        logger.debug("Inserted %s rows", len(batch))
