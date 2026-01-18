"""
后台采集线程：按配置的品种和周期从 MT5 持续拉取，写入缓存与 TimescaleDB。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, List

from src.config import IngestSettings
from src.clients.mt5_market import MT5MarketClient, MT5MarketError
from src.core.market_service import MarketDataService
from src.persistence.storage_writer import StorageWriter
from src.utils.common import ohlc_key, timeframe_seconds, timeframe_interval

logger = logging.getLogger(__name__)


class BackgroundIngestor:
    """长驻采集器：控制 tick/K 线的采集节奏与落库。"""

    def __init__(
        self,
        service: MarketDataService,
        storage: StorageWriter,
        ingest_settings: IngestSettings,
    ):
        self.settings = ingest_settings
        self.service = service
        self.storage = storage
        self.client: MT5MarketClient = service.client
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_tick_time: Dict[str, datetime] = {}
        self._last_ohlc_time: Dict[str, datetime] = {}
        self._last_intrabar_snapshot: Dict[str, tuple] = {}
        self._ohlc_lock = threading.Lock()  # 保护 _last_ohlc_time 的并发读写
        self._backfill_progress: Dict[str, datetime] = {}
        self._backfill_cutoff: Dict[str, datetime] = {}
        self._backfill_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._init_backfill_progress()
        if self._backfill_progress:
            self._backfill_thread = threading.Thread(
                target=self._backfill_async, name="mt5-backfill", daemon=True
            )
            self._backfill_thread.start()
        self._thread = threading.Thread(target=self._run, name="mt5-ingestor", daemon=True)
        self._thread.start()
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
            # 根据设置tick_interval以及实际耗时计算睡眠时间，避免过快循环。
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
        now = time.time()
        for tf in self.settings.ingest_ohlc_timeframes:
            key = ohlc_key(symbol, tf)
            # 计算当前周期间隔，决定下次采集时间。
            tf_interval = timeframe_interval(
                tf, self.settings.ingest_ohlc_interval, self.settings.ingest_ohlc_intervals
            )
            # 计算下次采集时间。
            due_at = next_ohlc_at.get(key, 0)
            # 当 time >= next_at 时才拉取数据；首轮 next_at 不 0 会拉取。
            if now < due_at:
                continue

            last_ts = self._get_last_ohlc_time(key)
            bars: List = []
            try:
                if last_ts:
                    # 若长时间未采集，持 last_ts 之后补齐（含当前未收盘）
                    start_time = last_ts + timedelta(seconds=timeframe_seconds(tf))
                    bars = self.client.get_ohlc_from(
                        symbol=symbol,
                        timeframe=tf,
                        start=start_time,
                        limit=self.settings.ohlc_backfill_limit,
                    )
                else:
                    # 初次拉取仅用于填充内存缓存，按缓存/默认窗口大小即可。
                    warmup_limit = max(
                        self.service.market_settings.ohlc_limit,
                        self.service.market_settings.ohlc_cache_limit,
                    )
                    # 首次拉取从当前位置开始的 K 线数据
                    bars = self.client.get_ohlc(symbol, tf, warmup_limit)
            except MT5MarketError as exc:
                logger.warning("Fetch OHLC failed for %s %s: %s", symbol, tf, exc)
                next_ohlc_at[key] = now + tf_interval
                continue

            if not bars:
                next_ohlc_at[key] = now + tf_interval
                continue

            # 默认仅写已收盘的 bar；盘中变更仅在内容变化时写入 intrabar 队列。
            to_write_closed: List[tuple] = []
            tf_seconds = timeframe_seconds(tf)
            # cutoff 基于 UTC，无 tzinfo，避免 naive/aware 比较错误。
            closed_cutoff = datetime.utcnow() - timedelta(seconds=tf_seconds)

            # 确保按时间排序，便于补齐缺口。
            bars = sorted(bars, key=lambda b: b.time)

            closed_bars: List = []
            new_events: List = []
            write_closed = last_ts is not None
            for bar in bars:
                is_closed = bar.time.replace(tzinfo=None) <= closed_cutoff
                if is_closed:
                    closed_bars.append(bar)
                    if last_ts is None or bar.time > last_ts:
                        new_events.append(bar)
                    # 用于写入数据库，write_closed 为 False（例如 warmup 阶段）时，不应该写 DB，但仍需要发事件。
                    if write_closed and (last_ts is None or bar.time > last_ts):
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
                snap_key = ohlc_key(symbol, tf)
                current = (bar.time, bar.open, bar.high, bar.low, bar.close, bar.volume)
                snapshot = self._last_intrabar_snapshot.get(snap_key)
                if snapshot is None or snapshot != current:
                    self._last_intrabar_snapshot[snap_key] = current
                    # 盘中快照用于 API 读取，不影响指标计算。
                    self.service.set_intrabar(symbol, tf, bar)
                    if not self.settings.intrabar_enabled:
                        continue
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

            if closed_bars:
                # 缓存写入只保留已收盘 K 线。
                self.service.set_ohlc_closed(symbol, tf, closed_bars)
                for bar in new_events:
                    self.service.enqueue_ohlc_closed_event(symbol, tf, bar.time)
                newest_closed = closed_bars[-1].time
                self._set_last_ohlc_time_if_newer(key, newest_closed)

            if to_write_closed:
                for row in to_write_closed:
                    self.storage.enqueue("ohlc", row)

            next_ohlc_at[key] = now + tf_interval

    # --- monitoring ---
    def queue_stats(self) -> dict:
        """返回各队列长度、上限以及缓冲大小，便于监控。"""
        stats = self.storage.stats()
        stats["threads"]["ingest_alive"] = self._thread.is_alive() if self._thread else False
        stats["threads"]["backfill_alive"] = self._backfill_thread.is_alive() if self._backfill_thread else False
        return stats

    def _init_backfill_progress(self) -> None:
        """启动时从数据库初始化回补起点与截止时间。"""
        try:
            writer = self.storage.db
        except Exception:
            return
        now = datetime.utcnow()
        for symbol in self.settings.ingest_symbols:
            for tf in self.settings.ingest_ohlc_timeframes:
                try:
                    last_ts = writer.last_ohlc_time(symbol, tf)
                except Exception:
                    continue
                if last_ts:
                    key = ohlc_key(symbol, tf)
                    self._backfill_progress[key] = last_ts
                    tf_seconds = timeframe_seconds(tf)
                    self._backfill_cutoff[key] = now - timedelta(seconds=tf_seconds)

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

    def _backfill_ohlc(self) -> None:
        writer = self.storage.db
        client = self.client
        for symbol in self.settings.ingest_symbols:
            for tf in self.settings.ingest_ohlc_timeframes:
                key = ohlc_key(symbol, tf)
                last_ts = self._backfill_progress.get(key)
                if last_ts is None:
                    continue
                tf_seconds = timeframe_seconds(tf)
                cutoff = self._backfill_cutoff.get(key)
                if cutoff is None:
                    logger.warning("Backfill cutoff missing for %s, skipping", key)
                    continue
                while not self._stop.is_set():
                    if last_ts >= cutoff:
                        break
                    start_time = last_ts + timedelta(seconds=tf_seconds)
                    try:
                        # 获取limit条数据，避免一次拉取过多。
                        bars = client.get_ohlc_from(
                            symbol=symbol,
                            timeframe=tf,
                            start=start_time,
                            limit=self.settings.ohlc_backfill_limit,
                        )
                    except MT5MarketError as exc:
                        logger.warning("Backfill OHLC failed for %s %s: %s", symbol, tf, exc)
                        break

                    if not bars:
                        break
                    bars = sorted(bars, key=lambda b: b.time)
                    # 只写入截止时间之前的 K 线，避免覆盖实时采集进度。
                    closed = [b for b in bars if b.time.replace(tzinfo=None) <= cutoff]
                    if not closed:
                        break
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
                            for bar in closed
                        ],
                        upsert=True,
                    )
                    new_last = closed[-1].time
                    if new_last <= last_ts:
                        break
                    last_ts = new_last
                    # 更新回补进度
                    self._backfill_progress[key] = last_ts

    def _backfill_async(self) -> None:
        try:
            self._backfill_ohlc()
        except Exception as exc:  # pragma: no cover - 防御
            logger.warning("Backfill thread failed: %s", exc)
