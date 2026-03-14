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
from psycopg2.extras import execute_batch
from psycopg2.pool import SimpleConnectionPool

from src.config import DBSettings, load_db_settings
from src.persistence.schema import (
    DDL_STATEMENTS,
    INSERT_TICKS_SQL,
    INSERT_QUOTES_SQL,
    INSERT_OHLC_SQL,
    UPSERT_OHLC_SQL,
    INSERT_INTRABAR_SQL,
)
from src.persistence.validator import DataValidator

logger = logging.getLogger(__name__)


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

    def write_ticks(self, rows: Iterable[Tuple[str, float, float, str]], page_size: int = 1000) -> None:
        """
        写入 tick 数据，自动验证数据有效性
        
        Args:
            rows: (symbol, price, volume, iso_time)
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
        
        self._batch(INSERT_TICKS_SQL, valid_rows, page_size=page_size)

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
                normalized.append((*row, {}))
            else:
                indicators = row[8] if row[8] is not None else {}
                normalized.append((*row[:8], indicators))
        
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
        sql = "SELECT max(time) FROM ticks WHERE symbol=%s"
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

    def fetch_recent_ticks(
        self,
        symbol: str,
        limit: int,
    ) -> List[Tuple[str, float, float, datetime]]:
        sql = (
            "SELECT symbol, price, volume, time "
            "FROM ("
            "    SELECT symbol, price, volume, time "
            "    FROM ticks WHERE symbol=%s "
            "    ORDER BY time DESC LIMIT %s"
            ") recent "
            "ORDER BY time ASC"
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
    ) -> List[Tuple[str, float, float, datetime]]:
        sql = "SELECT symbol, price, volume, time FROM ticks WHERE symbol=%s"
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
