"""历史 OHLC 数据加载器，从 TimescaleDB 流式读取。

新增组件：
- DataCache       进程级 OHLC 数据缓存，避免相同参数重复查询 DB
- CachingDataLoader  透明缓存包装层，drop-in 替换 HistoricalDataLoader
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Set, Tuple

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

    def load_child_bars(
        self,
        symbol: str,
        child_tf: str,
        start_time: datetime,
        end_time: datetime,
    ) -> List[OHLC]:
        """CachedDataLoader 不缓存子 TF 数据——回测 intrabar 场景应使用 CachingDataLoader。"""
        raise NotImplementedError(
            "CachedDataLoader does not support load_child_bars; "
            "use CachingDataLoader for intrabar backtests"
        )


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

    def load_child_bars(
        self,
        symbol: str,
        child_tf: str,
        start_time: datetime,
        end_time: datetime,
    ) -> List[OHLC]:
        """加载子 TF 的全部 bar（用于 intrabar 回测的双 TF 联合回放）。

        与 load_all_bars 语义相同，单独命名以明确调用意图。
        """
        return self.load_all_bars(symbol, child_tf, start_time, end_time)

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
            time=(
                time_val
                if isinstance(time_val, datetime)
                else datetime.fromisoformat(str(time_val))
            ),
            open=float(open_),
            high=float(high),
            low=float(low),
            close=float(close),
            volume=float(volume or 0.0),
            indicators=dict(indicators) if indicators else None,
        )


# ─── 进程级 OHLC 数据缓存 ──────────────────────────────────────────────────────


class DataCache:
    """进程级 OHLC 数据缓存，避免相同参数的回测任务重复查询 TimescaleDB。

    缓存策略：
    - warmup bars 按 (symbol, tf, start_iso, warmup_count) 索引
    - test bars   按 (symbol, tf, start_iso, end_iso) 索引
    - 各自独立缓存，支持部分命中（如相同 test 区间但不同 warmup 数量）
    - FIFO 淘汰（Python 3.7+ dict 保持插入顺序）
    - 历史数据不可变，无需 TTL

    线程安全：内置 Lock，多线程并发任务安全共享。
    """

    def __init__(self, max_entries: int = 6) -> None:
        self._lock = threading.Lock()
        self._warmup: Dict[Tuple, List[OHLC]] = {}
        self._test: Dict[Tuple, List[OHLC]] = {}
        self._max_entries = max_entries
        self._hits = 0
        self._misses = 0

    def get_or_load_warmup(
        self,
        loader: "HistoricalDataLoader",
        symbol: str,
        timeframe: str,
        start_time: datetime,
        warmup_bars: int,
    ) -> List[OHLC]:
        """返回 warmup bars，缓存命中时跳过 DB 查询。"""
        key: Tuple = (symbol, timeframe, start_time.isoformat(), warmup_bars)
        with self._lock:
            if key in self._warmup:
                self._hits += 1
                logger.debug(
                    "DataCache warmup HIT  %s/%s warmup=%d",
                    symbol,
                    timeframe,
                    warmup_bars,
                )
                return list(self._warmup[key])
        # Cache miss — load outside lock
        self._misses += 1
        logger.debug(
            "DataCache warmup MISS %s/%s warmup=%d", symbol, timeframe, warmup_bars
        )
        bars = loader.preload_warmup_bars(symbol, timeframe, start_time, warmup_bars)
        with self._lock:
            self._evict(self._warmup)
            self._warmup[key] = bars
        return list(bars)

    def get_or_load_test(
        self,
        loader: "HistoricalDataLoader",
        symbol: str,
        timeframe: str,
        start_time: datetime,
        end_time: datetime,
    ) -> List[OHLC]:
        """返回 test bars，缓存命中时跳过 DB 查询。"""
        key: Tuple = (symbol, timeframe, start_time.isoformat(), end_time.isoformat())
        with self._lock:
            if key in self._test:
                self._hits += 1
                logger.debug(
                    "DataCache test HIT  %s/%s [%s ~ %s]",
                    symbol,
                    timeframe,
                    start_time.date(),
                    end_time.date(),
                )
                return list(self._test[key])
        # Cache miss — load outside lock
        self._misses += 1
        logger.debug(
            "DataCache test MISS %s/%s [%s ~ %s]",
            symbol,
            timeframe,
            start_time.date(),
            end_time.date(),
        )
        bars = loader.load_all_bars(symbol, timeframe, start_time, end_time)
        with self._lock:
            self._evict(self._test)
            self._test[key] = bars
        return list(bars)

    def _evict(self, cache_dict: Dict) -> None:
        """FIFO 淘汰：缓存满时删除最老的条目。"""
        while len(cache_dict) >= self._max_entries:
            oldest = next(iter(cache_dict))
            del cache_dict[oldest]

    def invalidate(
        self,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
    ) -> int:
        """清除指定条件的缓存（数据重新导入后调用）。"""
        with self._lock:
            if symbol is None and timeframe is None:
                n = len(self._warmup) + len(self._test)
                self._warmup.clear()
                self._test.clear()
                return n

            def _match(k: Tuple) -> bool:
                return (symbol is None or k[0] == symbol) and (
                    timeframe is None or k[1] == timeframe
                )

            w_keys = [k for k in self._warmup if _match(k)]
            t_keys = [k for k in self._test if _match(k)]
            for k in w_keys:
                del self._warmup[k]
            for k in t_keys:
                del self._test[k]
            return len(w_keys) + len(t_keys)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "warmup_entries": len(self._warmup),
                "test_entries": len(self._test),
                "max_entries": self._max_entries,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": self._hits / total if total > 0 else 0.0,
            }


class CachingDataLoader:
    """HistoricalDataLoader 的透明缓存包装，与 HistoricalDataLoader 接口完全兼容。

    BacktestEngine / ParameterOptimizer / WalkForwardValidator 通过
    preload_warmup_bars() 和 load_all_bars() 访问数据，CachingDataLoader
    将这两个调用路由到 DataCache，相同参数只查询 DB 一次。
    """

    def __init__(
        self,
        inner: HistoricalDataLoader,
        cache: "DataCache",
    ) -> None:
        self._inner = inner
        self._cache = cache

    def preload_warmup_bars(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        warmup_bars: int = 200,
    ) -> List[OHLC]:
        return self._cache.get_or_load_warmup(
            self._inner, symbol, timeframe, start_time, warmup_bars
        )

    def load_all_bars(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        end_time: datetime,
    ) -> List[OHLC]:
        return self._cache.get_or_load_test(
            self._inner, symbol, timeframe, start_time, end_time
        )

    def load_child_bars(
        self,
        symbol: str,
        child_tf: str,
        start_time: datetime,
        end_time: datetime,
    ) -> List[OHLC]:
        """加载子 TF bars（经由 DataCache 缓存）。"""
        return self._cache.get_or_load_test(
            self._inner, symbol, child_tf, start_time, end_time
        )

    def load_bars(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        end_time: datetime,
        chunk_size: int = 1000,
    ) -> Iterator[List[OHLC]]:
        """流式分块加载（不经过缓存，适合单次大量数据场景）。"""
        yield from self._inner.load_bars(
            symbol, timeframe, start_time, end_time, chunk_size
        )

    @property
    def cache(self) -> "DataCache":
        return self._cache


# ─── 进程级单例 ──────────────────────────────────────────────────────────────

_shared_data_cache: Optional[DataCache] = None
_shared_data_cache_lock = threading.Lock()


def get_shared_data_cache(max_entries: int = 6) -> DataCache:
    """获取进程级共享 DataCache 单例。

    max_entries 仅在首次创建时生效；后续调用忽略该参数。
    """
    global _shared_data_cache
    if _shared_data_cache is None:
        with _shared_data_cache_lock:
            if _shared_data_cache is None:
                _shared_data_cache = DataCache(max_entries=max_entries)
    return _shared_data_cache
