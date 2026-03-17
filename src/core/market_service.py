"""
Business layer: in-memory cache backed by MT5, exposed to API and ingestion.
API defaults to reading from cache only; ingestion owns writes into the cache.
"""

from __future__ import annotations

from collections import deque
import logging
import queue
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Deque, Dict, List, Optional, Tuple

from src.config import MarketSettings, get_runtime_market_settings
from src.clients.mt5_market import MT5MarketClient, OHLC, Quote, SymbolInfo, Tick
from src.utils.common import ohlc_key, same_listener_reference

if TYPE_CHECKING:
    from src.persistence.storage_writer import StorageWriter

logger = logging.getLogger(__name__)


class MarketDataService:
    """
    In-memory market data service.

    - exposes quote / tick / OHLC query APIs
    - ingestion writes fresh data into cache
    - API and indicators read from the same cache snapshot
    """

    def __init__(
        self,
        client: MT5MarketClient,
        market_settings: Optional[MarketSettings] = None,
    ):
        self.market_settings = market_settings or get_runtime_market_settings()
        self.client = client
        self._lock = threading.RLock()
        self._tick_cache: Dict[str, Deque[Tick]] = {}
        self._quote_cache: Dict[str, Quote] = {}
        self._ohlc_closed_cache: Dict[str, List[OHLC]] = {}
        self._intrabar_cache: Dict[str, List[OHLC]] = {}
        self._intrabar_max_points = self.market_settings.intrabar_max_points
        self._storage_writer: Optional["StorageWriter"] = None
        self._ohlc_event_queue: queue.Queue = queue.Queue(
            maxsize=self.market_settings.ohlc_event_queue_size
        )
        self._ohlc_event_sink: Optional[Callable[[str, str, datetime], None]] = None
        self._ohlc_close_listeners: list[Callable[[str, str, datetime], None]] = []
        self._intrabar_listeners: list[Callable[[str, str, OHLC], None]] = []

    def list_symbols(self) -> List[str]:
        return self.client.list_symbols()

    def get_symbol_info(self, symbol: str) -> SymbolInfo:
        return self.client.get_symbol_info(symbol)

    def get_quote(self, symbol: Optional[str] = None) -> Optional[Quote]:
        symbol = symbol or self.market_settings.default_symbol
        with self._lock:
            cached = self._quote_cache.get(symbol)
            if cached:
                age = (self._utc_now() - self._as_utc(cached.time)).total_seconds()
                if age <= self.market_settings.quote_stale_seconds:
                    return cached
        return None

    def get_current_spread(self, symbol: Optional[str] = None) -> float:
        """Return current bid-ask spread in points. Returns 0.0 if no quote available."""
        quote = self.get_quote(symbol)
        if quote is None:
            return 0.0
        return abs(quote.ask - quote.bid)

    def get_quote_history(
        self,
        symbol: Optional[str],
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: int,
        strict: bool = False,
    ) -> List[Quote]:
        quotes, _source = self.get_quote_history_result(symbol, start_time, end_time, limit, strict=strict)
        return quotes

    def get_quote_history_result(
        self,
        symbol: Optional[str],
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: int,
        strict: bool = False,
    ) -> Tuple[List[Quote], str]:
        symbol = symbol or self.market_settings.default_symbol
        start_time = self._coerce_query_time(start_time)
        end_time = self._coerce_query_time(end_time)
        storage = self._storage()
        if storage is None:
            if strict:
                raise RuntimeError("Historical quote storage is unavailable")
            latest = self.get_quote(symbol)
            if latest is None:
                return [], "cache"
            return self._filter_quotes_by_time([latest], start_time, end_time), "cache"
        try:
            return (
                [
                    self._row_to_quote(row)
                    for row in storage.db.fetch_quotes(symbol, start_time, end_time, limit)
                ],
                "timescaledb",
            )
        except Exception:
            logger.exception("Failed to load quote history from storage for %s", symbol)
            if strict:
                raise RuntimeError(f"Failed to load quote history from storage for {symbol}")
            latest = self.get_quote(symbol)
            if latest is None:
                return [], "cache"
            return self._filter_quotes_by_time([latest], start_time, end_time), "cache"

    def get_ticks(self, symbol: Optional[str], limit: Optional[int] = None) -> List[Tick]:
        symbol = symbol or self.market_settings.default_symbol
        limit = limit or self.market_settings.tick_limit
        with self._lock:
            cache = self._tick_cache.get(symbol)
            cached_ticks = list(cache)[-limit:] if cache is not None else []
        if len(cached_ticks) >= limit:
            return cached_ticks
        storage = self._storage()
        if storage is None:
            return cached_ticks
        try:
            persisted = [
                self._row_to_tick(row)
                for row in storage.db.fetch_recent_ticks(symbol, limit)
            ]
        except Exception:
            logger.exception("Failed to load ticks from storage for %s", symbol)
            return cached_ticks
        merged = self._merge_ticks(persisted, cached_ticks)
        if merged:
            with self._lock:
                cache = self._tick_cache.setdefault(
                    symbol,
                    deque(maxlen=self.market_settings.tick_cache_size),
                )
                cache.clear()
                cache.extend(merged[-self.market_settings.tick_cache_size :])
        return merged[-limit:]

    def get_ticks_history(
        self,
        symbol: Optional[str],
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: int,
        strict: bool = False,
    ) -> List[Tick]:
        ticks, _source = self.get_ticks_history_result(symbol, start_time, end_time, limit, strict=strict)
        return ticks

    def get_ticks_history_result(
        self,
        symbol: Optional[str],
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: int,
        strict: bool = False,
    ) -> Tuple[List[Tick], str]:
        symbol = symbol or self.market_settings.default_symbol
        start_time = self._coerce_query_time(start_time)
        end_time = self._coerce_query_time(end_time)
        storage = self._storage()
        if storage is None:
            if strict:
                raise RuntimeError("Historical tick storage is unavailable")
            ticks = self.get_ticks(symbol, limit=limit)
            return self._filter_ticks_by_time(ticks, start_time, end_time), "cache"
        try:
            return (
                [
                    self._row_to_tick(row)
                    for row in storage.db.fetch_ticks(symbol, start_time, end_time, limit)
                ],
                "timescaledb",
            )
        except Exception:
            logger.exception("Failed to load tick history from storage for %s", symbol)
            if strict:
                raise RuntimeError(f"Failed to load tick history from storage for {symbol}")
            ticks = self.get_ticks(symbol, limit=limit)
            return self._filter_ticks_by_time(ticks, start_time, end_time), "cache"

    def health(self) -> dict:
        status = self.client.health()
        with self._lock:
            status["cached_quotes"] = len(self._quote_cache)
        status["timestamp"] = datetime.now(timezone.utc).isoformat()
        return status

    # --- ingestion hooks ---
    def set_quote(self, symbol: str, quote: Quote) -> None:
        with self._lock:
            self._quote_cache[symbol] = quote

    def extend_ticks(self, symbol: str, ticks: List[Tick]) -> None:
        with self._lock:
            cache = self._tick_cache.setdefault(
                symbol,
                deque(maxlen=self.market_settings.tick_cache_size),
            )
            cache.extend(ticks)

    def get_ohlc_closed(
        self,
        symbol: Optional[str],
        timeframe: str,
        limit: Optional[int] = None,
    ) -> List[OHLC]:
        symbol = symbol or self.market_settings.default_symbol
        limit = limit or self.market_settings.ohlc_limit
        key = ohlc_key(symbol, timeframe)
        with self._lock:
            cached = list(self._ohlc_closed_cache.get(key, []))
        if len(cached) >= limit:
            return cached[-limit:]
        storage = self._storage()
        if storage is None:
            return cached[-limit:]
        try:
            persisted = [
                self._row_to_ohlc(row)
                for row in storage.db.fetch_recent_ohlc(symbol, timeframe, limit)
            ]
        except Exception:
            logger.exception("Failed to load OHLC from storage for %s/%s", symbol, timeframe)
            return cached[-limit:]
        merged = self._merge_bars(persisted, cached)
        if merged:
            self.set_ohlc_closed(symbol, timeframe, merged)
        return merged[-limit:]

    def get_intrabar_series(self, symbol: Optional[str], timeframe: str) -> List[OHLC]:
        symbol = symbol or self.market_settings.default_symbol
        key = ohlc_key(symbol, timeframe)
        with self._lock:
            return list(self._intrabar_cache.get(key, []))

    def get_ohlc(
        self,
        symbol: Optional[str],
        timeframe: str,
        count: Optional[int] = None,
    ) -> List[OHLC]:
        """Compatibility wrapper for indicator services expecting get_ohlc()."""
        return self.get_ohlc_closed(symbol, timeframe, limit=count)

    def get_ohlc_window(
        self,
        symbol: Optional[str],
        timeframe: str,
        end_time: datetime,
        limit: int,
        strict: bool = False,
    ) -> List[OHLC]:
        symbol = symbol or self.market_settings.default_symbol
        end_time = self._coerce_query_time(end_time)
        key = ohlc_key(symbol, timeframe)
        with self._lock:
            cached = list(self._ohlc_closed_cache.get(key, []))

        cached_window = [bar for bar in cached if bar.time <= end_time]
        if cached_window and cached_window[-1].time == end_time and len(cached_window) >= limit:
            return cached_window[-limit:]

        storage = self._storage()
        if storage is None:
            if strict:
                raise RuntimeError(f"Historical OHLC storage is unavailable for {symbol}/{timeframe}")
            return cached_window[-limit:] if cached_window and cached_window[-1].time == end_time else []

        try:
            persisted = [
                self._row_to_ohlc(row)
                for row in storage.db.fetch_ohlc_before(symbol, timeframe, end_time, limit)
            ]
        except Exception:
            logger.exception(
                "Failed to load OHLC window from storage for %s/%s ending at %s",
                symbol,
                timeframe,
                end_time,
            )
            if strict:
                raise RuntimeError(f"Failed to load OHLC window from storage for {symbol}/{timeframe}")
            return cached_window[-limit:] if cached_window and cached_window[-1].time == end_time else []

        if persisted:
            self.set_ohlc_closed(symbol, timeframe, persisted)
            if persisted[-1].time == end_time:
                return persisted[-limit:]

        return cached_window[-limit:] if cached_window and cached_window[-1].time == end_time else []

    def get_latest_ohlc(self, symbol: Optional[str], timeframe: str) -> Optional[OHLC]:
        bars = self.get_ohlc_closed(symbol, timeframe, limit=1)
        return bars[-1] if bars else None

    def has_cached_ohlc(self, symbol: Optional[str], timeframe: str, minimum_bars: int = 1) -> bool:
        symbol = symbol or self.market_settings.default_symbol
        key = ohlc_key(symbol, timeframe)
        with self._lock:
            return len(self._ohlc_closed_cache.get(key, [])) >= max(1, minimum_bars)

    def get_ohlc_history(
        self,
        symbol: Optional[str],
        timeframe: str,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: int,
        strict: bool = False,
    ) -> List[OHLC]:
        bars, _source = self.get_ohlc_history_result(
            symbol,
            timeframe,
            start_time,
            end_time,
            limit,
            strict=strict,
        )
        return bars

    def get_ohlc_history_result(
        self,
        symbol: Optional[str],
        timeframe: str,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: int,
        strict: bool = False,
    ) -> Tuple[List[OHLC], str]:
        symbol = symbol or self.market_settings.default_symbol
        start_time = self._coerce_query_time(start_time)
        end_time = self._coerce_query_time(end_time)
        storage = self._storage()
        if storage is None:
            if strict:
                raise RuntimeError(f"Historical OHLC storage is unavailable for {symbol}/{timeframe}")
            bars = self.get_ohlc_closed(symbol, timeframe, limit=limit)
            return self._filter_bars_by_time(bars, start_time, end_time), "cache"
        try:
            bars = [
                self._row_to_ohlc(row)
                for row in storage.db.fetch_ohlc_range(symbol, timeframe, start_time, end_time, limit)
            ]
        except Exception:
            logger.exception("Failed to load OHLC history from storage for %s/%s", symbol, timeframe)
            if strict:
                raise RuntimeError(f"Failed to load OHLC history from storage for {symbol}/{timeframe}")
            bars = self.get_ohlc_closed(symbol, timeframe, limit=limit)
            return self._filter_bars_by_time(bars, start_time, end_time), "cache"
        if bars:
            self.set_ohlc_closed(symbol, timeframe, bars)
        return bars, "timescaledb"

    def set_ohlc_closed(self, symbol: str, timeframe: str, bars: List[OHLC]) -> None:
        if not bars:
            return
        key = ohlc_key(symbol, timeframe)
        with self._lock:
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
        with self._lock:
            existing = self._intrabar_cache.get(key)
            if not existing or existing[-1].time != bar.time:
                self._intrabar_cache[key] = [bar]
            else:
                existing.append(bar)
                if len(existing) > self._intrabar_max_points:
                    del existing[:-self._intrabar_max_points]
        for listener in list(self._intrabar_listeners):
            try:
                listener(symbol, timeframe, bar)
            except Exception:
                logger.exception("Failed to publish intrabar event for %s/%s at %s", symbol, timeframe, bar.time)

    def attach_storage(self, storage_writer: Optional["StorageWriter"]) -> None:
        self._storage_writer = storage_writer


    def add_ohlc_close_listener(self, listener: Callable[[str, str, datetime], None]) -> None:
        with self._lock:
            self._ohlc_close_listeners.append(listener)

    def remove_ohlc_close_listener(self, listener: Callable[[str, str, datetime], None]) -> None:
        with self._lock:
            self._ohlc_close_listeners = [
                item
                for item in self._ohlc_close_listeners
                if not same_listener_reference(item, listener)
            ]

    def add_intrabar_listener(self, listener: Callable[[str, str, OHLC], None]) -> None:
        with self._lock:
            self._intrabar_listeners.append(listener)

    def remove_intrabar_listener(self, listener: Callable[[str, str, OHLC], None]) -> None:
        with self._lock:
            self._intrabar_listeners = [
                item
                for item in self._intrabar_listeners
                if not same_listener_reference(item, listener)
            ]

    def set_ohlc_event_sink(
        self,
        sink: Optional[Callable[[str, str, datetime], None]],
    ) -> None:
        """Register a durable event sink for closed-bar notifications."""
        self._ohlc_event_sink = sink

    def enqueue_ohlc_closed_event(self, symbol: str, timeframe: str, bar_time: datetime) -> None:
        for listener in list(self._ohlc_close_listeners):
            try:
                listener(symbol, timeframe, bar_time)
            except Exception:
                logger.exception("Failed to notify OHLC close listener for %s/%s at %s", symbol, timeframe, bar_time)
        if self._ohlc_event_sink is not None:
            try:
                self._ohlc_event_sink(symbol, timeframe, bar_time)
                return
            except Exception:
                logger.exception(
                    "Failed to publish OHLC close event for %s/%s at %s",
                    symbol,
                    timeframe,
                    bar_time,
                )
        try:
            self._ohlc_event_queue.put_nowait((symbol, timeframe, bar_time))
        except queue.Full:
            logger.warning(
                "Dropped in-memory OHLC close event because the queue is full: %s/%s %s",
                symbol,
                timeframe,
                bar_time,
            )

    def get_ohlc_event(self, timeout: Optional[float] = None) -> Optional[tuple]:
        try:
            return self._ohlc_event_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def update_ohlc_indicators(
        self,
        symbol: str,
        timeframe: str,
        bar_time: datetime,
        indicators: Dict[str, Any],
    ) -> None:
        key = ohlc_key(symbol, timeframe)
        with self._lock:
            cached = self._ohlc_closed_cache.get(key)
            if not cached:
                return
            for bar in reversed(cached):
                if bar.time != bar_time:
                    continue
                existing = getattr(bar, "indicators", None) or {}
                if indicators:
                    merged = dict(existing)
                    for name, payload in indicators.items():
                        if isinstance(payload, dict) and isinstance(merged.get(name), dict):
                            merged[name] = {**merged[name], **payload}
                        else:
                            merged[name] = payload
                    bar.indicators = merged
                elif bar.indicators is None:
                    bar.indicators = {}
                return

    def latest_indicators(self, symbol: str, timeframe: str) -> Dict[str, Any]:
        bars = self.get_ohlc_closed(symbol, timeframe, limit=1)
        if not bars:
            return {}
        return dict(getattr(bars[-1], "indicators", {}) or {})

    def _storage(self) -> Optional["StorageWriter"]:
        return self._storage_writer

    def _normalize_time(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return self.client._to_tz(value.astimezone(timezone.utc))

    def _coerce_query_time(self, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _row_to_tick(self, row: tuple) -> Tick:
        if len(row) >= 5:
            symbol, price, volume, time, time_msc = row[:5]
        else:
            symbol, price, volume, time = row
            time_msc = None
        return Tick(
            symbol=symbol,
            price=float(price),
            volume=float(volume or 0.0),
            time=self._normalize_time(time),
            time_msc=int(time_msc) if time_msc is not None else None,
        )

    def _row_to_quote(self, row: tuple) -> Quote:
        symbol, bid, ask, last, volume, time = row
        bid_value = float(bid)
        ask_value = float(ask)
        return Quote(
            symbol=symbol,
            bid=bid_value,
            ask=ask_value,
            last=MT5MarketClient._normalize_last_price(
                bid_value,
                ask_value,
                float(last or 0.0),
            ),
            volume=float(volume or 0.0),
            time=self._normalize_time(time),
        )

    def _row_to_ohlc(self, row: tuple) -> OHLC:
        symbol, timeframe, open_, high, low, close, volume, time, indicators = row
        return OHLC(
            symbol=symbol,
            timeframe=timeframe,
            time=self._normalize_time(time),
            open=float(open_),
            high=float(high),
            low=float(low),
            close=float(close),
            volume=float(volume or 0.0),
            indicators=dict(indicators or {}),
        )

    @staticmethod
    def _merge_ticks(base: List[Tick], overlay: List[Tick]) -> List[Tick]:
        merged = {
            (tick.time, tick.time_msc, tick.price, tick.volume): tick
            for tick in base
        }
        for tick in overlay:
            merged[(tick.time, tick.time_msc, tick.price, tick.volume)] = tick
        return sorted(
            merged.values(),
            key=lambda item: (
                item.time_msc if item.time_msc is not None else int(item.time.timestamp() * 1000),
                item.time,
            ),
        )

    @staticmethod
    def _merge_bars(base: List[OHLC], overlay: List[OHLC]) -> List[OHLC]:
        merged = {bar.time: bar for bar in base}
        for bar in overlay:
            existing = merged.get(bar.time)
            if existing and getattr(existing, "indicators", None) and getattr(bar, "indicators", None):
                bar.indicators = {**existing.indicators, **bar.indicators}
            elif existing and getattr(existing, "indicators", None) and getattr(bar, "indicators", None) is None:
                bar.indicators = dict(existing.indicators)
            merged[bar.time] = bar
        return sorted(merged.values(), key=lambda item: item.time)

    @staticmethod
    def _filter_ticks_by_time(
        ticks: List[Tick],
        start_time: Optional[datetime],
        end_time: Optional[datetime],
    ) -> List[Tick]:
        result = ticks
        if start_time is not None:
            result = [tick for tick in result if tick.time >= start_time]
        if end_time is not None:
            result = [tick for tick in result if tick.time <= end_time]
        return result

    @staticmethod
    def _filter_quotes_by_time(
        quotes: List[Quote],
        start_time: Optional[datetime],
        end_time: Optional[datetime],
    ) -> List[Quote]:
        result = quotes
        if start_time is not None:
            result = [quote for quote in result if quote.time >= start_time]
        if end_time is not None:
            result = [quote for quote in result if quote.time <= end_time]
        return result

    @staticmethod
    def _filter_bars_by_time(
        bars: List[OHLC],
        start_time: Optional[datetime],
        end_time: Optional[datetime],
    ) -> List[OHLC]:
        result = bars
        if start_time is not None:
            result = [bar for bar in result if bar.time >= start_time]
        if end_time is not None:
            result = [bar for bar in result if bar.time <= end_time]
        return result
