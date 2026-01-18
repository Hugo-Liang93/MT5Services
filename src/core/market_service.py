"""
Business layer: in-memory cache backed by MT5, exposed to API and ingestion.
API 默认只读缓存，写入由后台采集器负责，避免冲突。
"""

from __future__ import annotations

from collections import deque
import queue
from datetime import datetime, timedelta
from typing import Deque, Dict, List, Optional

from src.config import MarketSettings, load_market_settings
from src.clients.mt5_market import MT5MarketClient, Quote, Tick, OHLC, SymbolInfo
from src.utils.common import ohlc_key


class MarketDataService:
    """
    内存行情服务：
    - 对外提供 quote / ticks / ohlc 查询接口
    - 采集器写入最新数据；API 只读缓存，避免写冲突
    - OHLC 缓存由采集器写入，服务层不回源补齐
    """

    def __init__(
        self,
        client: MT5MarketClient,
        market_settings: Optional[MarketSettings] = None,
    ):
        self.market_settings = market_settings or load_market_settings()
        self.client = client
        self._tick_cache: Dict[str, Deque[Tick]] = {}
        self._quote_cache: Dict[str, Quote] = {}
        self._ohlc_closed_cache: Dict[str, List[OHLC]] = {}
        # 每个 key 只保留当前这根 K 线区间内的盘中变化；一旦 bar.time 变了（新 K 线），就把旧区间的序列清空并重新开始记录。
        self._intrabar_cache: Dict[str, List[OHLC]] = {}
        self._intrabar_max_points = self.market_settings.intrabar_max_points
        self._ohlc_event_queue: queue.Queue = queue.Queue(
            maxsize=self.market_settings.ohlc_event_queue_size
        )

    def list_symbols(self) -> List[str]:
        return self.client.list_symbols()

    def get_symbol_info(self, symbol: str) -> SymbolInfo:
        return self.client.get_symbol_info(symbol)

    def get_quote(self, symbol: Optional[str]) -> Optional[Quote]:
        symbol = symbol or self.market_settings.default_symbol
        cached = self._quote_cache.get(symbol)
        # 只读缓存，未过期则直接返回。
        if cached:
            age = (datetime.utcnow() - cached.time.replace(tzinfo=None)).total_seconds()
            if age <= self.market_settings.quote_stale_seconds:
                return cached
        return None

    def get_ticks(self, symbol: Optional[str], limit: Optional[int] = None) -> List[Tick]:
        symbol = symbol or self.market_settings.default_symbol
        limit = limit or self.market_settings.tick_limit
        cache = self._tick_cache.get(symbol)

        if cache is not None:
            return list(cache)[-limit:]

        return []

    def health(self) -> dict:
        status = self.client.health()
        status["cached_quotes"] = len(self._quote_cache)
        status["timestamp"] = datetime.utcnow().isoformat() + "Z"
        return status

    # --- ingestion hooks: 供采集器写缓存 ---
    def set_quote(self, symbol: str, quote: Quote) -> None:
        self._quote_cache[symbol] = quote

    def extend_ticks(self, symbol: str, ticks: List[Tick]) -> None:
        cache = self._tick_cache.setdefault(symbol, deque(maxlen=self.market_settings.tick_cache_size))
        cache.extend(ticks)

    def get_ohlc_closed(self, symbol: Optional[str], timeframe: str, limit: Optional[int] = None) -> List[OHLC]:
        symbol = symbol or self.market_settings.default_symbol
        limit = limit or self.market_settings.ohlc_limit
        key = ohlc_key(symbol, timeframe)
        cached = self._ohlc_closed_cache.get(key, [])
        if len(cached) >= limit:
            return cached[-limit:]
        return cached[-limit:]

    def get_intrabar_series(self, symbol: Optional[str], timeframe: str) -> List[OHLC]:
        symbol = symbol or self.market_settings.default_symbol
        key = ohlc_key(symbol, timeframe)
        return list(self._intrabar_cache.get(key, []))

    def set_ohlc_closed(self, symbol: str, timeframe: str, bars: List[OHLC]) -> None:
        if not bars:
            return
        key = ohlc_key(symbol, timeframe)
        existing = self._ohlc_closed_cache.get(key, [])
        merged = {bar.time: bar for bar in existing}
        for bar in bars:
            existing_bar = merged.get(bar.time)
            incoming_indicators = getattr(bar, "indicators", None)
            existing_indicators = getattr(existing_bar, "indicators", None) if existing_bar else None
            if incoming_indicators is None and existing_indicators:
                bar.indicators = dict(existing_indicators)
            elif incoming_indicators is None:
                bar.indicators = {}
            elif existing_indicators:
                bar.indicators = {**existing_indicators, **incoming_indicators}
            merged[bar.time] = bar
        sorted_bars = sorted(merged.values(), key=lambda b: b.time)
        keep_limit = max(self.market_settings.ohlc_limit, self.market_settings.ohlc_cache_limit)
        self._ohlc_closed_cache[key] = sorted_bars[-keep_limit:]

    def set_intrabar(self, symbol: str, timeframe: str, bar: OHLC) -> None:
        key = ohlc_key(symbol, timeframe)
        existing = self._intrabar_cache.get(key)
        if not existing or existing[-1].time != bar.time:
            self._intrabar_cache[key] = [bar]
        else:
            existing.append(bar)
            if len(existing) > self._intrabar_max_points:
                del existing[:-self._intrabar_max_points]

    def enqueue_ohlc_closed_event(self, symbol: str, timeframe: str, bar_time: datetime) -> None:
        try:
            # event结构: (symbol, timeframe, bar_time)
            self._ohlc_event_queue.put_nowait((symbol, timeframe, bar_time))
        except queue.Full:
            return

    def get_ohlc_event(self, timeout: Optional[float] = None) -> Optional[tuple]:
        try:
            return self._ohlc_event_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def update_ohlc_indicators(
        self, symbol: str, timeframe: str, bar_time: datetime, indicators: Dict[str, float]
    ) -> None:
        key = ohlc_key(symbol, timeframe)
        cached = self._ohlc_closed_cache.get(key)
        if not cached:
            return
        for bar in reversed(cached):
            if bar.time != bar_time:
                continue
            existing = getattr(bar, "indicators", None) or {}
            if indicators:
                bar.indicators = {**existing, **indicators}
            elif bar.indicators is None:
                bar.indicators = {}
            return
