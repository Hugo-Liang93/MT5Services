"""历史 OHLC 数据加载器，从 TimescaleDB 流式读取。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Iterator, List, Optional, Set

from src.clients.mt5_market import OHLC

if TYPE_CHECKING:
    from src.persistence.repositories.market_repo import MarketRepository

logger = logging.getLogger(__name__)

# fetch_ohlc_range 单次查询上限
_MAX_QUERY_LIMIT = 100_000


class CachedDataLoader:
    """预加载数据的缓存包装器，避免优化器多次迭代重复查询 DB。

    将 warmup bars 和 test bars 缓存在内存中，
    每次调用返回列表副本（防止被调用方修改原始数据）。
    """

    def __init__(
        self,
        warmup_bars: List[OHLC],
        test_bars: List[OHLC],
    ) -> None:
        self._warmup_bars = warmup_bars
        self._test_bars = test_bars

    def preload_warmup_bars(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        warmup_bars: int = 200,
    ) -> List[OHLC]:
        """返回缓存的 warmup bars 副本。"""
        return list(self._warmup_bars)

    def load_all_bars(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        end_time: datetime,
    ) -> List[OHLC]:
        """返回缓存的 test bars 副本。"""
        return list(self._test_bars)


class HistoricalDataLoader:
    """从 TimescaleDB 加载历史 OHLC 数据。

    复用 MarketRepository 的查询方法，支持分块加载和 warmup 预加载。
    """

    def __init__(self, market_repo: "MarketRepository") -> None:
        self._repo = market_repo

    def load_bars(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        end_time: datetime,
        chunk_size: int = 1000,
    ) -> Iterator[List[OHLC]]:
        """分块加载 OHLC 数据。

        Args:
            symbol: 交易品种
            timeframe: 时间框架
            start_time: 起始时间（含）
            end_time: 结束时间（含）
            chunk_size: 每次返回的 bar 数量

        Yields:
            每次 chunk_size 根 OHLC bar 的列表
        """
        cursor_time: Optional[datetime] = start_time
        while cursor_time is not None and cursor_time <= end_time:
            rows = self._repo.fetch_ohlc(
                symbol=symbol,
                timeframe=timeframe,
                start_time=cursor_time,
                limit=chunk_size,
            )
            if not rows:
                break

            bars = [self._row_to_ohlc(r) for r in rows]
            # 过滤超出 end_time 的 bar
            bars = [b for b in bars if b.time <= end_time]
            if not bars:
                break

            yield bars
            # 推进游标到最后一根 bar 之后
            cursor_time = bars[-1].time
            # fetch_ohlc 使用 time > start_time，所以不会重复

            if len(rows) < chunk_size:
                break  # 没有更多数据

    def load_all_bars(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        end_time: datetime,
    ) -> List[OHLC]:
        """一次性加载全部 bar。适合较短回测周期。"""
        rows = self._repo.fetch_ohlc_range(
            symbol=symbol,
            timeframe=timeframe,
            start_time=start_time,
            end_time=end_time,
            limit=_MAX_QUERY_LIMIT,
        )
        bars = [self._row_to_ohlc(r) for r in rows]
        bars = self._ensure_sorted(bars)
        logger.info(
            "Loaded %d bars for %s/%s [%s ~ %s]",
            len(bars),
            symbol,
            timeframe,
            start_time.isoformat(),
            end_time.isoformat(),
        )
        return bars

    def preload_warmup_bars(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        warmup_bars: int = 200,
    ) -> List[OHLC]:
        """加载 start_time 之前的 warmup bar，用于指标冷启动。"""
        rows = self._repo.fetch_ohlc_before(
            symbol=symbol,
            timeframe=timeframe,
            end_time=start_time,
            limit=warmup_bars,
        )
        bars = [self._row_to_ohlc(r) for r in rows]
        # 排除 start_time 本身（避免重复）
        bars = [b for b in bars if b.time < start_time]
        bars = self._ensure_sorted(bars)
        logger.info(
            "Loaded %d warmup bars for %s/%s before %s",
            len(bars),
            symbol,
            timeframe,
            start_time.isoformat(),
        )
        return bars

    @staticmethod
    def _ensure_sorted(bars: List[OHLC]) -> List[OHLC]:
        """确保 bars 按时间升序排列，乱序时自动排序并去重。"""
        if len(bars) <= 1:
            return bars
        # 检查是否已排序
        is_sorted = all(bars[i].time <= bars[i + 1].time for i in range(len(bars) - 1))
        if not is_sorted:
            logger.warning(
                "OHLC bars not in chronological order, sorting %d bars", len(bars)
            )
            bars = sorted(bars, key=lambda b: b.time)
        # 去重（相同时间戳只保留最后一条）
        seen_times: Set[datetime] = set()
        unique: List[OHLC] = []
        for bar in bars:
            if bar.time not in seen_times:
                seen_times.add(bar.time)
                unique.append(bar)
            else:
                logger.debug("Duplicate bar at %s removed", bar.time)
        return unique

    @staticmethod
    def _row_to_ohlc(row: tuple) -> OHLC:
        """将数据库行转换为 OHLC 对象。

        DB 行格式: (symbol, timeframe, open, high, low, close, volume, time, indicators)
        """
        symbol, timeframe, open_, high, low, close, volume, time_val, indicators = row
        return OHLC(
            symbol=str(symbol),
            timeframe=str(timeframe),
            time=time_val if isinstance(time_val, datetime) else datetime.fromisoformat(str(time_val)),
            open=float(open_),
            high=float(high),
            low=float(low),
            close=float(close),
            volume=float(volume or 0.0),
            indicators=dict(indicators) if indicators else None,
        )
