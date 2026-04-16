"""
后台采集线程：按配置的品种和周期从 MT5 持续拉取，写入缓存与 TimescaleDB。
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, TypeVar

_T = TypeVar("_T")

from src.clients.mt5_market import OHLC, MT5MarketClient, MT5MarketError
from src.config import IngestSettings
from src.market import MarketDataService
from src.market.synthesis import synthesize_parent_bar
from src.persistence.storage_writer import StorageWriter
from src.utils.common import ohlc_key, timeframe_interval, timeframe_seconds

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
        self._ohlc_lock = threading.Lock()  # 保护 _last_ohlc_time 的并发读写
        self._backfill_progress: Dict[str, datetime] = {}
        self._backfill_cutoff: Dict[str, datetime] = {}
        self._backfill_thread: Optional[threading.Thread] = None
        self._symbol_cursor = 0
        # MT5 调用可能因连接冻结而无限期阻塞；用单线程 executor 提交并设置超时，
        # 超时后主循环继续，abandoned future 的底层线程最终自行结束。
        self._fetch_timeout: float = float(ingest_settings.connection_timeout)
        self._fetch_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="mt5-ohlc-fetch"
        )
        # 连续失败退避：品种连续采集失败 threshold 次后，
        # 进入指数退避冷却期，防止 MT5 网络抖动时日志被刷爆。
        # 退避公式：cooldown = base × 2^(retry_count - 1)，上限 max_cooldown
        # 参数从 ingest.ini [error_recovery] 或 [ingest] section 加载。
        self._symbol_error_threshold = int(ingest_settings.symbol_error_threshold)
        self._symbol_cooldown_seconds = float(ingest_settings.symbol_cooldown_seconds)
        self._symbol_max_cooldown_seconds = float(
            ingest_settings.symbol_max_cooldown_seconds
        )
        self._symbol_error_counts: Dict[str, int] = {}
        self._symbol_backoff_until: Dict[str, float] = {}
        self._symbol_backoff_count: Dict[str, int] = {}  # 累计退避轮次
        # 回补完成标志：回补线程结束后设为 True，供 SignalRuntime 判断 warmup 状态
        self._backfill_done = threading.Event()
        # Intrabar trigger: parent_tf → trigger_tf（例 {"H1": "M5"}）。
        # 当 trigger_tf bar close 时，从已确认的 trigger_tf bars 合成 parent_tf
        # 当前 bar 并注入 intrabar 管道。已配置的 parent_tf 不再轮询 MT5。
        self._intrabar_trigger_map: dict[str, str] = {}
        # 反查表：trigger_tf → [parent_tf, ...]
        self._intrabar_trigger_reverse: dict[str, list[str]] = {}
        # 断更检测：每个 (symbol, parent_tf) 最后一次成功合成的单调时钟
        self._last_synthesis_at: Dict[str, float] = {}
        self._synthesis_count: Dict[str, int] = {}

    @property
    def is_backfilling(self) -> bool:
        """回补线程是否仍在运行。无回补任务时视为已完成。"""
        return not self._backfill_done.is_set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._backfill_done.clear()
        self._init_backfill_progress()
        if self._backfill_progress:
            self._backfill_thread = threading.Thread(
                target=self._backfill_async, name="mt5-backfill", daemon=True
            )
            self._backfill_thread.start()
        else:
            # 无回补任务，直接标记完成
            self._backfill_done.set()
        self._thread = threading.Thread(
            target=self._run, name="mt5-ingestor", daemon=True
        )
        self._thread.start()
        windowed = (
            0 < self.settings.max_concurrent_symbols < len(self.settings.ingest_symbols)
        )
        logger.info(
            "Background ingestor started%s",
            (
                f" (symbol window {self.settings.max_concurrent_symbols}/{len(self.settings.ingest_symbols)} per cycle)"
                if windowed
                else ""
            ),
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._backfill_thread:
            self._backfill_thread.join(timeout=10)
        self._fetch_executor.shutdown(wait=False)
        # 确保 StorageWriter 队列中的残余数据被刷出，避免关机丢数据
        if self.storage is not None:
            try:
                self.storage.flush(timeout=5.0)
            except Exception:
                logger.warning(
                    "Ingestor: StorageWriter flush failed on stop", exc_info=True
                )
        logger.info("Background ingestor stopped")

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _fetch_ohlc_with_timeout(self, fn: Callable[[], Any]) -> Any:
        """在独立线程中执行 MT5 OHLC 调用，超时则抛出 MT5MarketError。

        MT5 底层 IPC 调用在连接冻结时可能无限阻塞；通过 Future + timeout
        让主循环在 _fetch_timeout 秒后继续处理其他品种，避免全局挂死。
        超时后 abandoned future 的后台线程会在 MT5 API 自行返回后结束。
        """
        future = self._fetch_executor.submit(fn)
        try:
            return future.result(timeout=self._fetch_timeout)
        except concurrent.futures.TimeoutError:
            raise MT5MarketError(
                f"MT5 OHLC fetch timed out after {self._fetch_timeout}s"
            )

    def _run(self) -> None:
        poll_interval = self.settings.ingest_poll_interval
        next_ohlc_at: Dict[str, float] = {}
        while not self._stop.is_set():
            start_loop = time.time()
            now = time.time()
            for symbol in self._symbols_for_cycle():
                # 冷却期内跳过连续多次失败的品种，避免 CPU 空转。
                if now < self._symbol_backoff_until.get(symbol, 0.0):
                    continue
                try:
                    self._ingest_quote(symbol)
                    self._ingest_ticks(symbol)
                    self._ingest_ohlc(symbol, next_ohlc_at)
                    # 本轮成功，重置错误计数和退避轮次。
                    self._symbol_error_counts.pop(symbol, None)
                    self._symbol_backoff_count.pop(symbol, None)
                except MT5MarketError as exc:
                    count = self._symbol_error_counts.get(symbol, 0) + 1
                    self._symbol_error_counts[symbol] = count
                    logger.warning(
                        "Ingest error for %s (consecutive=%d): %s", symbol, count, exc
                    )
                    if count >= self._symbol_error_threshold:
                        backoff_round = self._symbol_backoff_count.get(symbol, 0)
                        cooldown = min(
                            self._symbol_cooldown_seconds * (2**backoff_round),
                            self._symbol_max_cooldown_seconds,
                        )
                        self._symbol_backoff_until[symbol] = now + cooldown
                        self._symbol_error_counts.pop(symbol, None)
                        self._symbol_backoff_count[symbol] = backoff_round + 1
                        logger.error(
                            "Symbol %s hit %d consecutive errors; "
                            "exponential backoff cooldown=%.0fs (round=%d).",
                            symbol,
                            self._symbol_error_threshold,
                            cooldown,
                            backoff_round + 1,
                        )
                except Exception as exc:  # pragma: no cover - 防御
                    logger.exception("Unexpected ingest error for %s: %s", symbol, exc)
            # 保持采集节奏，扣除本轮耗时。
            elapsed = time.time() - start_loop
            # 根据 poll_interval 以及实际耗时计算睡眠时间，避免过快循环。
            sleep_for = max(0, poll_interval - elapsed)
            self._stop.wait(sleep_for)

    def _symbols_for_cycle(self) -> List[str]:
        symbols = list(self.settings.ingest_symbols)
        if not symbols:
            return []

        window = max(1, int(self.settings.max_concurrent_symbols))
        if window >= len(symbols):
            return symbols

        start = self._symbol_cursor % len(symbols)
        selected = [
            symbols[(start + offset) % len(symbols)] for offset in range(window)
        ]
        self._symbol_cursor = (start + window) % len(symbols)
        return selected

    def _ingest_quote(self, symbol: str) -> None:
        # 最新报价：用于 API 快速读取，缓存更新；写入缓存区，低频批量落库。
        quote = self.client.get_quote(symbol)
        self.service.set_quote(symbol, quote)
        if self.storage.settings.quote_flush_enabled:
            self.storage.enqueue(
                "quotes",
                (
                    quote.symbol,
                    quote.bid,
                    quote.ask,
                    quote.last,
                    quote.volume,
                    quote.time.isoformat(),
                ),
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
            logger.debug("Fetched %s ticks for %s", len(new_ticks), symbol)
            for item in [
                (
                    tick.symbol,
                    tick.price,
                    tick.volume,
                    tick.time.isoformat(),
                    tick.time_msc,
                )
                for tick in new_ticks
            ]:
                self.storage.enqueue("ticks", item)

    def _ingest_ohlc(self, symbol: str, next_ohlc_at: Dict[str, float]) -> None:
        now = time.time()
        for tf in self.settings.ingest_ohlc_timeframes:
            key = ohlc_key(symbol, tf)
            # 计算当前周期间隔，决定下次采集时间。
            tf_interval = timeframe_interval(
                tf,
                self.settings.ingest_ohlc_interval,
                self.settings.ingest_ohlc_intervals,
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
                    bars = self._fetch_ohlc_with_timeout(
                        lambda s=symbol, t=tf, st=start_time: self.client.get_ohlc_from(
                            symbol=s,
                            timeframe=t,
                            start=st,
                            limit=self.settings.ohlc_backfill_limit,
                        )
                    )
                else:
                    # 初次拉取仅用于填充内存缓存，按缓存/默认窗口大小即可。
                    warmup_limit = max(
                        self.service.market_settings.ohlc_limit,
                        self.service.market_settings.ohlc_cache_limit,
                    )
                    # 首次拉取从当前位置开始的 K 线数据
                    bars = self._fetch_ohlc_with_timeout(
                        lambda s=symbol, t=tf, lim=warmup_limit: self.client.get_ohlc(
                            s, t, lim
                        )
                    )
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
            closed_cutoff = datetime.now(timezone.utc) - timedelta(seconds=tf_seconds)

            # 确保按时间排序，便于补齐缺口。
            bars = sorted(bars, key=lambda b: b.time)

            closed_bars: List = []
            new_events: List = []
            for bar in bars:
                bar_time_utc = (
                    bar.time.replace(tzinfo=timezone.utc)
                    if bar.time.tzinfo is None
                    else bar.time.astimezone(timezone.utc)
                )
                is_closed = bar_time_utc <= closed_cutoff
                if is_closed:
                    closed_bars.append(bar)
                    if last_ts is None or bar.time > last_ts:
                        new_events.append(bar)
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

                # 未收盘 bar 由 trigger 模式合成处理，此处跳过。

            if closed_bars:
                # 缓存写入只保留已收盘 K 线。
                self.service.set_ohlc_closed(symbol, tf, closed_bars)
                for bar in new_events:
                    self.service.enqueue_ohlc_closed_event(symbol, tf, bar.time)
                newest_closed = closed_bars[-1].time
                self._set_last_ohlc_time_if_newer(key, newest_closed)
                # 子 TF close → 合成父 TF intrabar bar
                if new_events and tf in self._intrabar_trigger_reverse:
                    for parent_tf in self._intrabar_trigger_reverse[tf]:
                        self._synthesize_parent_intrabar(
                            symbol, tf, parent_tf, newest_closed
                        )

            if to_write_closed:
                for row in to_write_closed:
                    self.storage.enqueue("ohlc", row)

            next_ohlc_at[key] = now + tf_interval

    # --- intrabar trigger: synthesize parent TF bar from child TF closes ---

    def _synthesize_parent_intrabar(
        self,
        symbol: str,
        trigger_tf: str,
        parent_tf: str,
        trigger_bar_time: datetime,
    ) -> None:
        """从子 TF closed bars 合成父 TF 当前未收盘 bar，注入 intrabar 管道。

        在 _ingest_ohlc 检测到子 TF bar close 后立即调用。
        从 _ohlc_closed_cache 读子 TF bars（纯内存，零 I/O），
        合成父 TF 当前 bar 后调 set_intrabar() 注入现有管道。
        """
        parent_tf_secs = timeframe_seconds(parent_tf)
        trigger_tf_secs = timeframe_seconds(trigger_tf)
        if parent_tf_secs <= 0 or trigger_tf_secs <= 0:
            return

        # 计算父 TF 当前 bar 的开盘时间（向下对齐）
        trigger_ts = trigger_bar_time.timestamp()
        parent_bar_open_ts = trigger_ts - (trigger_ts % parent_tf_secs)
        parent_bar_open = datetime.fromtimestamp(parent_bar_open_ts, tz=timezone.utc)

        # 读取子 TF confirmed bars，筛选当前父 bar 区间内的
        max_child_bars = parent_tf_secs // trigger_tf_secs + 2
        child_bars = self.service.get_ohlc_closed(
            symbol, trigger_tf, limit=max_child_bars
        )
        bars_in_range = [b for b in child_bars if b.time >= parent_bar_open]
        if not bars_in_range:
            return

        synthesis_count = self._synthesis_count.get(ohlc_key(symbol, parent_tf), 0) + 1
        synthesized_at = datetime.now(timezone.utc)

        # 合成父 TF 当前未收盘 bar（复用共享纯函数）
        synthesized = synthesize_parent_bar(
            bars_in_range,
            symbol,
            parent_tf,
            parent_bar_open,
        )

        # 注入现有 intrabar 管道
        self.service.set_intrabar(
            symbol,
            parent_tf,
            synthesized,
            metadata={
                "trigger_tf": trigger_tf,
                "bar_time": parent_bar_open.isoformat(),
                "synthesized_at": synthesized_at.isoformat(),
                "expected_interval_seconds": trigger_tf_secs,
                "stale_threshold_seconds": trigger_tf_secs * 3,
                "last_child_bar_time": trigger_bar_time.isoformat(),
                "child_bar_count": len(bars_in_range),
                "count": synthesis_count,
            },
        )

        # 持久化到 TimescaleDB（与轮询路径对齐）
        if self.settings.intrabar_enabled:
            recorded_at = datetime.now(timezone.utc).isoformat()
            self.storage.enqueue(
                "intrabar",
                (
                    synthesized.symbol,
                    synthesized.timeframe,
                    synthesized.open,
                    synthesized.high,
                    synthesized.low,
                    synthesized.close,
                    synthesized.volume,
                    synthesized.time.isoformat(),
                    recorded_at,
                ),
            )

        # 断更检测：记录成功合成时间和计数
        synthesis_key = ohlc_key(symbol, parent_tf)
        self._last_synthesis_at[synthesis_key] = time.monotonic()
        self._synthesis_count[synthesis_key] = synthesis_count

        logger.debug(
            "Ingestor: %s %s close → synthesized %s intrabar "
            "(child_bars=%d, close=%.5f)",
            symbol,
            trigger_tf,
            parent_tf,
            len(bars_in_range),
            synthesized.close,
        )

    def set_intrabar_trigger_map(self, trigger_map: dict[str, str]) -> None:
        """设置 intrabar trigger 映射。

        Args:
            trigger_map: parent_tf → trigger_tf，例 {"H1": "M5", "H4": "M15"}
        """
        self._intrabar_trigger_map = dict(trigger_map)
        reverse: dict[str, list[str]] = {}
        for parent_tf, trigger_tf in trigger_map.items():
            reverse.setdefault(trigger_tf, []).append(parent_tf)
        self._intrabar_trigger_reverse = reverse
        if trigger_map:
            logger.info(
                "Ingestor intrabar trigger configured: %s (reverse: %s)",
                trigger_map,
                reverse,
            )

    # --- monitoring ---
    def queue_stats(self) -> dict:
        """返回各队列长度、上限以及缓冲大小，便于监控。"""
        stats = self.storage.stats()
        stats["threads"]["ingest_alive"] = (
            self._thread.is_alive() if self._thread else False
        )
        stats["threads"]["backfill_alive"] = (
            self._backfill_thread.is_alive() if self._backfill_thread else False
        )
        stats["intrabar_synthesis"] = self._intrabar_synthesis_stats()
        return stats

    def _intrabar_synthesis_stats(self) -> dict:
        """返回每个 (symbol, parent_tf) 的 intrabar 合成状态。

        包含合成计数、距上次合成的时间间隔、以及是否疑似断更。
        断更判定：距上次合成超过子 TF 周期的 3 倍。
        """
        now = time.monotonic()
        result: Dict[str, dict] = {}
        for parent_tf, trigger_tf in self._intrabar_trigger_map.items():
            expected_interval = timeframe_seconds(trigger_tf)
            stale_threshold = expected_interval * 3
            for symbol in self.settings.ingest_symbols:
                key = ohlc_key(symbol, parent_tf)
                last_at = self._last_synthesis_at.get(key)
                count = self._synthesis_count.get(key, 0)
                if last_at is not None:
                    age = now - last_at
                    stale = age > stale_threshold
                    status = "stale" if stale else "healthy"
                else:
                    age = None
                    stale = False
                    status = "warming_up"
                result[key] = {
                    "trigger_tf": trigger_tf,
                    "count": count,
                    "expected_interval_seconds": expected_interval,
                    "stale_threshold_seconds": stale_threshold,
                    "last_age_seconds": round(age, 1) if age is not None else None,
                    "stale": stale,
                    "status": status,
                }
        return result

    def intrabar_synthesis_summary(self) -> dict[str, dict[str, Any]]:
        return self._intrabar_synthesis_stats()

    def _init_backfill_progress(self) -> None:
        """启动时从数据库初始化回补起点与截止时间。"""
        try:
            writer = self.storage.db
        except Exception:
            return
        now = datetime.now(timezone.utc)
        for symbol in self.settings.ingest_symbols:
            try:
                last_tick = writer.last_tick_time(symbol)
            except Exception:
                last_tick = None
            if last_tick:
                self._last_tick_time[symbol] = last_tick
        for symbol in self.settings.ingest_symbols:
            for tf in self.settings.ingest_ohlc_timeframes:
                try:
                    last_ts = writer.last_ohlc_time(symbol, tf)
                except Exception:
                    continue
                if last_ts:
                    key = ohlc_key(symbol, tf)
                    tf_seconds = timeframe_seconds(tf)
                    # Gap 检测：如果 DB 中最新数据太旧（超过 backfill_limit 个周期），
                    # 忽略旧的 last_ts，让 Ingestor 走初次拉取路径重新初始化。
                    gap_seconds = (now - last_ts).total_seconds()
                    max_gap = tf_seconds * self.settings.ohlc_backfill_limit
                    if gap_seconds > max_gap:
                        logger.warning(
                            "OHLC gap detected for %s %s: last_ts=%s (%.1fh ago), "
                            "exceeds max_gap=%ds. Resetting to fresh init.",
                            symbol,
                            tf,
                            last_ts,
                            gap_seconds / 3600,
                            max_gap,
                        )
                        continue  # 不设 _last_ohlc_time → 走初次拉取路径
                    self._set_last_ohlc_time_if_newer(key, last_ts)
                    self._backfill_progress[key] = last_ts
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
                        logger.warning(
                            "Backfill OHLC failed for %s %s: %s", symbol, tf, exc
                        )
                        break

                    if not bars:
                        break
                    bars = sorted(bars, key=lambda b: b.time)
                    # 只写入截止时间之前的 K 线，避免覆盖实时采集进度。
                    closed = [
                        b
                        for b in bars
                        if (
                            b.time.replace(tzinfo=timezone.utc)
                            if b.time.tzinfo is None
                            else b.time.astimezone(timezone.utc)
                        )
                        <= cutoff
                    ]
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
                    self.service.set_ohlc_closed(symbol, tf, closed)
                    for bar in closed:
                        self.service.enqueue_ohlc_closed_event(symbol, tf, bar.time)
                    self._set_last_ohlc_time_if_newer(key, closed[-1].time)
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
        finally:
            self._backfill_done.set()
            logger.info("Backfill completed, warmup barrier lifted")
