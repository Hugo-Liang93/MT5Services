"""
后台采集线程：按配置的品种和周期从 MT5 持续拉取，写入缓存与 TimescaleDB。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, List

from src.config import IngestSettings, load_ingest_settings
from src.clients.mt5_market import MT5MarketClient, MT5MarketError
from src.core.market_service import MarketDataService
from src.persistence.db import TimescaleWriter
from src.persistence.storage_writer import StorageWriter

logger = logging.getLogger(__name__)


class BackgroundIngestor:
    """长驻采集器：控制 tick/K 线的采集节奏与落库。"""

    def __init__(
        self,
        service: MarketDataService,
        storage: Optional[StorageWriter] = None,
        ingest_settings: Optional[IngestSettings] = None,
    ):
        self.settings = ingest_settings or load_ingest_settings()
        self.service = service
        self.storage = storage or StorageWriter(TimescaleWriter())
        self.client: MT5MarketClient = service.client
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_tick_time: Dict[str, datetime] = {}
        self._last_ohlc_time: Dict[str, datetime] = {}
        self._last_intrabar_snapshot: Dict[str, tuple] = {}
        self._ohlc_lock = threading.Lock()  # 保护 _last_ohlc_time 的并发读写，避免回补/实时线程竞争
        self._backfill_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mt5-ingestor", daemon=True)
        self._thread.start()
        # Backfill 放在后台线程执行，避免阻塞实时采集。
        self._backfill_thread = threading.Thread(target=self._backfill_async, name="mt5-backfill", daemon=True)
        self._backfill_thread.start()
        logger.info("Background ingestor started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._backfill_thread:
            self._backfill_thread.join(timeout=10)
        logger.info("Background ingestor stopped")

    def _run(self) -> None:
        tick_interval = self.settings.ingest_tick_interval
        next_ohlc_at: Dict[str, float] = {}
        while not self._stop.is_set():
            start_loop = time.time()
            for symbol in self.settings.ingest_symbols:
                try:
                    self._ingest_quote(symbol)
                    self._ingest_ticks(symbol)
                    self._ingest_ohlc(symbol, next_ohlc_at)
                except MT5MarketError as exc:
                    logger.warning("Ingest error for %s: %s", symbol, exc)
                except Exception as exc:  # pragma: no cover - 防御
                    logger.exception("Unexpected ingest error for %s: %s", symbol, exc)
            # 保持采集节奏，扣除本轮耗时。
            elapsed = time.time() - start_loop
            sleep_for = max(0, tick_interval - elapsed)
            self._stop.wait(sleep_for)

    def _ingest_quote(self, symbol: str) -> None:
        # 最新报价：用于 API 快速读取，缓存更新；写入缓存区，低频批量落库。
        quote = self.client.get_quote(symbol)
        self.service.set_quote(symbol, quote)
        self.storage.enqueue(
            "quotes", (quote.symbol, quote.bid, quote.ask, quote.last, quote.volume, quote.time.isoformat())
        )

    def _ingest_ticks(self, symbol: str) -> None:
        # 增量拉取 tick：记录上次时间戳，避免重复写入。
        since = self._last_tick_time.get(symbol)
        start_time = since + timedelta(seconds=0.001) if since else None
        ticks = self.client.get_ticks(symbol, self.settings.tick_cache_size, start_time)

        new_ticks = []
        for tick in ticks:
            if since and tick.time <= since:
                continue
            new_ticks.append(tick)

        if new_ticks:
            self._last_tick_time[symbol] = new_ticks[-1].time
            self.service.extend_ticks(symbol, new_ticks)
            logger.info("Fetched %s ticks for %s", len(new_ticks), symbol)
            for item in [
                (tick.symbol, tick.price, tick.volume, tick.time.isoformat())
                for tick in new_ticks
            ]:
                self.storage.enqueue("ticks", item)

    def _ingest_ohlc(self, symbol: str, next_ohlc_at: Dict[str, float]) -> None:
        # 按时间框架定期拉取最新 2 根K线（当前未收盘+ 上一根）
        now = time.time()
        for tf in self.settings.ingest_ohlc_timeframes:
            key = f"{symbol}-{tf}"
            tf_interval = self._get_tf_interval(tf)
            due_at = next_ohlc_at.get(key, 0)
            # 当 time >= next_at 时才拉取数据；首轮 next_at 不 0 会拉取。
            if now < due_at:
                continue

            last_ts = self._get_last_ohlc_time(key)
            bars: List = []
            try:
                if last_ts:
                    # 若长时间未采集，持 last_ts 之后补齐（含当前未收盘）
                    start_time = last_ts + timedelta(seconds=self._timeframe_seconds(tf))
                    bars = self.client.get_ohlc_from(
                        symbol=symbol,
                        timeframe=tf,
                        start=start_time,
                        limit=self.settings.ohlc_backfill_limit,
                    )
                else:
                    bars = self.client.get_ohlc(symbol, tf, self.settings.ohlc_backfill_limit)
            except MT5MarketError as exc:
                logger.warning("Fetch OHLC failed for %s %s: %s", symbol, tf, exc)
                next_ohlc_at[key] = now + tf_interval
                continue

            if not bars:
                next_ohlc_at[key] = now + tf_interval
                continue

            # 缓存写入，内部会按配置保留窗口。
            self.service.set_ohlc(symbol, tf, bars)
            # 默认仅写已收盘的 bar；盘中变更仅在内容变化时写入 intrabar 队列。
            to_write_closed: List[tuple] = []
            tf_seconds = self._timeframe_seconds(tf)
            # cutoff 基于 UTC，无 tzinfo，避免 naive/aware 比较错误。
            closed_cutoff = datetime.utcnow() - timedelta(seconds=tf_seconds)

            # 确保按时间排序，便于补齐缺口。
            bars = sorted(bars, key=lambda b: b.time)

            for bar in bars:
                is_closed = bar.time.replace(tzinfo=None) <= closed_cutoff
                if is_closed:
                    if last_ts is None or bar.time > last_ts:
                        to_write_closed.append(
                            (
                                bar.symbol,
                                bar.timeframe,
                                bar.open,
                                bar.high,
                                bar.low,
                                bar.close,
                                bar.volume,
                                bar.time.isoformat(),
                            )
                        )
                    continue

                # 盘中未收盘 bar：仅当变更时记录到 intrabar 队列。
                if not self.settings.intrabar_enabled:
                    continue
                snap_key = f"{symbol}:{tf}"
                current = (bar.open, bar.high, bar.low, bar.close, bar.volume)
                snapshot = self._last_intrabar_snapshot.get(snap_key)
                if snapshot is None or snapshot != current:
                    self._last_intrabar_snapshot[snap_key] = current
                    recorded_at = datetime.utcnow().isoformat()
                    self.storage.enqueue(
                        "intrabar",
                        (
                            bar.symbol,
                            bar.timeframe,
                            bar.open,
                            bar.high,
                            bar.low,
                            bar.close,
                            bar.volume,
                            bar.time.isoformat(),
                            recorded_at,
                        ),
                    )

            if to_write_closed:
                newest = max([bar.time for bar in bars])
                self._set_last_ohlc_time_if_newer(key, newest)
                for row in to_write_closed:
                    self.storage.enqueue("ohlc", row)

            next_ohlc_at[key] = now + tf_interval

    # --- utils ---
    def _timeframe_seconds(self, tf: str) -> int:
        tf_upper = tf.upper()
        mapping = {
            "M1": 60,
            "M5": 300,
            "M15": 900,
            "M30": 1800,
            "H1": 3600,
            "H4": 14400,
            "D1": 86400,
        }
        return mapping.get(tf_upper, 60)

    def _get_tf_interval(self, tf: str) -> float:
        tf_upper = tf.upper()
        if tf_upper in self.settings.ingest_ohlc_intervals:
            return float(self.settings.ingest_ohlc_intervals[tf_upper])
        return self.settings.ingest_ohlc_interval

    # --- monitoring ---
    def queue_stats(self) -> dict:
        """返回各队列长度、上限以及缓冲大小，便于监控。"""
        stats = self.storage.stats()
        stats["threads"]["ingest_alive"] = self._thread.is_alive() if self._thread else False
        stats["threads"]["backfill_alive"] = self._backfill_thread.is_alive() if self._backfill_thread else False
        return stats

    def _get_last_ohlc_time(self, key: str) -> Optional[datetime]:
        # 统一从锁内读取，防止回补线程与实时线程竞争。
        with self._ohlc_lock:
            return self._last_ohlc_time.get(key)

    def _set_last_ohlc_time_if_newer(self, key: str, ts: datetime) -> None:
        # 仅在时间戳更新时写入，避免回退实时进度。
        with self._ohlc_lock:
            current = self._last_ohlc_time.get(key)
            if current is None or ts > current:
                self._last_ohlc_time[key] = ts

    def _backfill_ohlc(self, writer: Optional[TimescaleWriter] = None, client: Optional[MT5MarketClient] = None) -> None:
        """基于数据库最新时间戳补充已收盘K线，避免启动断档。"""
        writer = writer or self.storage.db
        client = client or self.client
        for symbol in self.settings.ingest_symbols:
            for tf in self.settings.ingest_ohlc_timeframes:
                last_ts = writer.last_ohlc_time(symbol, tf)
                tf_seconds = self._timeframe_seconds(tf)
                lookback = tf_seconds * self.settings.ohlc_backfill_limit
                now_dt = datetime.utcnow()
                # 回补窗口覆盖最近 lookback 区间，确保补齐中间遗漏的收盘 bar。
                start_time = (last_ts - timedelta(seconds=lookback)) if last_ts else (now_dt - timedelta(seconds=lookback))
                try:
                    bars = client.get_ohlc_from(
                        symbol=symbol,
                        timeframe=tf,
                        start=start_time,
                        limit=self.settings.ohlc_backfill_limit,
                    )
                except MT5MarketError as exc:
                    logger.warning("Backfill OHLC failed for %s %s: %s", symbol, tf, exc)
                    continue

                # 只写已收盘的 bars，并按时间排序。统一基于 UTC，无 tzinfo。
                cutoff = datetime.utcnow() - timedelta(seconds=tf_seconds)
                bars = [b for b in bars if b.time.replace(tzinfo=None) <= cutoff]
                if not bars:
                    continue
                bars = sorted(bars, key=lambda b: b.time)
                writer.write_ohlc(
                    [
                        (
                            bar.symbol,
                            bar.timeframe,
                            bar.open,
                            bar.high,
                            bar.low,
                            bar.close,
                            bar.volume,
                            bar.time.isoformat(),
                        )
                        for bar in bars
                    ],
                    upsert=True,
                )
                key = f"{symbol}-{tf}"
                self._set_last_ohlc_time_if_newer(key, bars[-1].time)
                logger.info("Backfilled %s bars for %s %s (up to %s)", len(bars), symbol, tf, bars[-1].time.isoformat())

    def _backfill_async(self) -> None:
        """使用独立连接在后台执行 backfill，避免阻塞实时采集。"""
        try:
            local_writer = TimescaleWriter(self.settings)
            local_client = MT5MarketClient(self.settings)
            local_writer.init_schema()
            # 共享同一时区配置。
            self._backfill_ohlc(writer=local_writer, client=local_client)
        except Exception as exc:  # pragma: no cover - 防御
            logger.warning("Backfill thread failed: %s", exc)
