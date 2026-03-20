from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Iterable, List, Optional, Tuple

from src.persistence.schema import (
    INSERT_INTRABAR_SQL,
    INSERT_OHLC_SQL,
    INSERT_QUOTES_SQL,
    INSERT_TICKS_SQL,
    UPSERT_OHLC_SQL,
)
from src.persistence.validator import DataValidator

if TYPE_CHECKING:
    from src.persistence.db import TimescaleWriter

logger = logging.getLogger(__name__)
TICK_ORDER_SQL = "COALESCE(time_msc, FLOOR(EXTRACT(EPOCH FROM time) * 1000)::bigint)"


class MarketRepository:
    def __init__(self, writer: "TimescaleWriter"):
        self._writer = writer

    def write_ticks(self, rows: Iterable[Tuple], page_size: int = 1000) -> None:
        rows_list = list(rows)
        if not rows_list:
            return

        valid_rows = DataValidator.filter_valid_ticks(rows_list)
        if not valid_rows:
            logger.warning("No valid tick data to write")
            return

        invalid_count = len(rows_list) - len(valid_rows)
        if invalid_count > 0:
            logger.warning("Dropped %s invalid tick rows", invalid_count)

        normalized = []
        for row in valid_rows:
            if len(row) >= 5:
                normalized.append((row[0], row[1], row[2], row[3], row[4]))
                continue

            time_value = datetime.fromisoformat(str(row[3]).replace("Z", "+00:00"))
            normalized.append((row[0], row[1], row[2], row[3], int(time_value.timestamp() * 1000)))

        self._writer._batch(INSERT_TICKS_SQL, normalized, page_size=page_size)

    def write_quotes(
        self,
        rows: Iterable[Tuple[str, float, float, float, float, str]],
        page_size: int = 1000,
    ) -> None:
        rows_list = list(rows)
        if not rows_list:
            return

        valid_rows = DataValidator.filter_valid_quotes(rows_list)
        if not valid_rows:
            logger.warning("No valid quote data to write")
            return

        invalid_count = len(rows_list) - len(valid_rows)
        if invalid_count > 0:
            logger.warning("Dropped %s invalid quote rows", invalid_count)

        self._writer._batch(INSERT_QUOTES_SQL, valid_rows, page_size=page_size)

    def write_ohlc(self, rows: Iterable[Tuple], upsert: bool = False, page_size: int = 1000) -> None:
        rows_list = list(rows)
        if not rows_list:
            return

        valid_rows = DataValidator.filter_valid_ohlc(rows_list, upsert)
        if not valid_rows:
            logger.warning("No valid OHLC data to write")
            return

        invalid_count = len(rows_list) - len(valid_rows)
        if invalid_count > 0:
            logger.warning("Dropped %s invalid OHLC rows", invalid_count)

        normalized = []
        for row in valid_rows:
            if len(row) == 8:
                normalized.append((*row, self._writer._json({})))
            else:
                indicators = row[8] if row[8] is not None else {}
                normalized.append((*row[:8], self._writer._json(indicators)))

        sql = UPSERT_OHLC_SQL if upsert else INSERT_OHLC_SQL
        self._writer._batch(sql, normalized, page_size=page_size)

    def write_ohlc_intrabar(
        self,
        rows: Iterable[Tuple[str, str, float, float, float, float, float, str, str]],
        page_size: int = 1000,
    ) -> None:
        rows_list = list(rows)
        if not rows_list:
            return

        valid_rows = DataValidator.filter_valid_intrabar(rows_list)
        if not valid_rows:
            logger.warning("No valid intrabar data to write")
            return

        invalid_count = len(rows_list) - len(valid_rows)
        if invalid_count > 0:
            logger.warning("Dropped %s invalid intrabar rows", invalid_count)

        self._writer._batch(INSERT_INTRABAR_SQL, valid_rows, page_size=page_size)

    def last_ohlc_time(self, symbol: str, timeframe: str) -> Optional[datetime]:
        sql = "SELECT max(time) FROM ohlc WHERE symbol=%s AND timeframe=%s"
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (symbol, timeframe))
            row = cur.fetchone()
            return row[0] if row and row[0] else None

    def last_tick_time(self, symbol: str) -> Optional[datetime]:
        sql = (
            "SELECT time FROM ticks WHERE symbol=%s "
            f"ORDER BY {TICK_ORDER_SQL} DESC, time DESC LIMIT 1"
        )
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (symbol,))
            row = cur.fetchone()
            return row[0] if row and row[0] else None

    def last_quote_time(self, symbol: str) -> Optional[datetime]:
        sql = "SELECT max(time) FROM quotes WHERE symbol=%s"
        with self._writer.connection() as conn, conn.cursor() as cur:
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
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def fetch_recent_ohlc(self, symbol: str, timeframe: str, limit: int) -> List[Tuple]:
        sql = (
            "SELECT symbol, timeframe, open, high, low, close, volume, time, indicators "
            "FROM ("
            "    SELECT symbol, timeframe, open, high, low, close, volume, time, indicators "
            "    FROM ohlc WHERE symbol=%s AND timeframe=%s "
            "    ORDER BY time DESC LIMIT %s"
            ") recent "
            "ORDER BY time ASC"
        )
        with self._writer.connection() as conn, conn.cursor() as cur:
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
        with self._writer.connection() as conn, conn.cursor() as cur:
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
        with self._writer.connection() as conn, conn.cursor() as cur:
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
        with self._writer.connection() as conn, conn.cursor() as cur:
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
        with self._writer.connection() as conn, conn.cursor() as cur:
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
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
