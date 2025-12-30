"""
Business layer: in-memory cache backed by MT5, exposed to API and ingestion.
API 默认只读缓存，写入由后台采集器负责，避免冲突。
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from typing import Deque, Dict, List, Optional

from src.config import MarketSettings, load_market_settings
from src.clients.mt5_market import MT5MarketClient, Quote, Tick, OHLC, SymbolInfo


class MarketDataService:
    """
    内存行情服务：
    - 对外提供 quote / ticks / ohlc 查询接口
    - 采集器写入最新数据；API 只读缓存，避免写冲突
    - 缓存缺失时可直接向 MT5 拉取一次作为兜底，但不回写缓存
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
        self._ohlc_cache: Dict[str, List[OHLC]] = {}

    def list_symbols(self) -> List[str]:
        return self.client.list_symbols()

    def get_symbol_info(self, symbol: str) -> SymbolInfo:
        return self.client.get_symbol_info(symbol)

    def get_quote(self, symbol: Optional[str]) -> Quote:
        symbol = symbol or self.market_settings.default_symbol
        cached = self._quote_cache.get(symbol)
        # 只读缓存，未过期则直接返回。
        if cached:
            age = (datetime.utcnow() - cached.time.replace(tzinfo=None)).total_seconds()
            if age <= self.market_settings.quote_stale_seconds:
                return cached
        # 缓存缺失或过期：直接拉取一次作为兜底，不写回缓存，避免与采集写冲突。
        return self.client.get_quote(symbol)

    def get_ticks(self, symbol: Optional[str], limit: Optional[int] = None) -> List[Tick]:
        symbol = symbol or self.market_settings.default_symbol
        limit = limit or self.market_settings.tick_limit
        cache = self._tick_cache.get(symbol)

        if cache is not None:
            return list(cache)[-limit:]

        # 缓存不存在时兜底拉取一次，不写缓存。
        return self.client.get_ticks(symbol, limit)

    def get_ohlc(self, symbol: Optional[str], timeframe: str, limit: Optional[int] = None) -> List[OHLC]:
        symbol = symbol or self.market_settings.default_symbol
        limit = limit or self.market_settings.ohlc_limit
        key = self._ohlc_key(symbol, timeframe)
        if key in self._ohlc_cache:
            cached = self._ohlc_cache[key]
            if len(cached) >= limit:
                return cached[-limit:]
        # 缓存缺失或不足时兜底拉取一次，可补充缓存。
        fetched = self.client.get_ohlc(symbol, timeframe, limit)
        if fetched:
            self.set_ohlc(symbol, timeframe, fetched)
            return fetched[-limit:]
        return self._ohlc_cache.get(key, [])[-limit:] if key in self._ohlc_cache else []

    def last_quote(self, symbol: Optional[str]) -> Optional[Quote]:
        symbol = symbol or self.market_settings.default_symbol
        return self._quote_cache.get(symbol)

    def snapshot(self, symbol: Optional[str]) -> dict:
        """便捷快照，用于下游消费或监控。"""
        symbol = symbol or self.market_settings.default_symbol
        quote = self.last_quote(symbol) or self.get_quote(symbol)
        return {
            "symbol": symbol,
            "bid": quote.bid,
            "ask": quote.ask,
            "last": quote.last,
            "volume": quote.volume,
            "time": quote.time.isoformat(),
        }

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

    def set_ohlc(self, symbol: str, timeframe: str, bars: List[OHLC]) -> None:
        key = self._ohlc_key(symbol, timeframe)
        existing = self._ohlc_cache.get(key, [])
        merged = {bar.time: bar for bar in existing}
        for bar in bars:
            merged[bar.time] = bar
        sorted_bars = sorted(merged.values(), key=lambda b: b.time)
        keep_limit = max(self.market_settings.ohlc_limit, self.market_settings.ohlc_cache_limit)
        self._ohlc_cache[key] = sorted_bars[-keep_limit:]

    def _ohlc_key(self, symbol: str, timeframe: str) -> str:
        return f"{symbol}:{timeframe}"
