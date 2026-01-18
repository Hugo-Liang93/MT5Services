"""
指标计算后台线程（配置驱动）
- 任务列表来自配置文件（ini/JSON），支持定时热加载
- 统一约定：指标函数签名为 fn(bars, params) -> dict 或标量
- 与 ingestion 解耦：只读 MarketDataService 缓存
"""

from __future__ import annotations

import importlib
import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.core.market_service import MarketDataService
from src.utils.common import ohlc_key, timeframe_seconds
from src.config import IndicatorSettings, load_indicator_settings, load_indicator_tasks, load_db_settings
from src.indicators.types import IndicatorTask
from src.clients.mt5_market import OHLC
from src.persistence.db import TimescaleWriter
from src.persistence.storage_writer import StorageWriter


class IndicatorSnapshot:
    """单个 symbol+timeframe 的指标快照。"""

    def __init__(self, symbol: str, timeframe: str, data: dict):
        self.symbol = symbol
        self.timeframe = timeframe
        self.data = data  # 指标键值对


@dataclass
class CompiledIndicatorTask:
    name: str
    func: Callable
    params: Dict[str, Any]
    min_bars: int


class IndicatorWorker:
    """可配置的指标计算后台线程（事件驱动 + 滑动窗口）。"""

    def __init__(
        self,
        service: MarketDataService,
        symbols: List[str],
        timeframes: List[str],
        tasks: List[IndicatorTask],
        indicator_settings: Optional[IndicatorSettings] = None,
        storage: Optional[StorageWriter] = None,
    ):
        # 通过在deps中实例化并注入单例，实现全局唯一，从而达到拉齐获取数据与指标一致的目的
        self.service = service
        self.symbols = symbols
        self.timeframes = timeframes
        self.tasks = tasks
        self.settings = indicator_settings or load_indicator_settings()
        self.poll_seconds = self.settings.poll_seconds
        self.ohlc_cache_limit = self.service.market_settings.ohlc_cache_limit
        self.config_path = self.settings.config_path
        self.reload_interval = self.settings.reload_interval
        self.storage = storage

        # 线程控制
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._backfill_thread: Optional[threading.Thread] = None
        # 共享状态锁（热加载/计算并发保护）
        self._lock = threading.Lock()
        # 最新的指标快照（供API或内部查询）
        self._snapshots: Dict[str, IndicatorSnapshot] = {}
        # 配置热加载的时间戳控制
        self._last_config_mtime: Optional[float] = None
        self._last_reload_ts: float = 0.0
        # 指标函数缓存，避免重复 import
        self._func_cache: Dict[str, Callable] = {}
        # 时间指针（key -> bar_time）
        self._last_computed_time: Dict[str, datetime] = {}  # key->最近计算完成的收盘K线时间
        self._last_seen_time: Dict[str, datetime] = {}  # key->最近在服务缓存里看到的收盘K线时间
        # 本地滑动窗口缓存（key -> deque[OHLC]）
        self._local_cache: Dict[str, deque] = {}
        # 编译后的任务和窗口需求
        self._compiled_tasks: List[CompiledIndicatorTask] = []
        self._required_bars_count: int = 0

    # 更新task配置,防止多线程读写冲突,在热加载的时候防止冲突
    def update_tasks(self, tasks: List[IndicatorTask]) -> None:
        """编译任务配置并统计所需最小窗口长度。"""
        compiled: List[CompiledIndicatorTask] = []
        required = 0
        for task in tasks:
            fn = self._get_func(task.func_path)
            if fn is None:
                continue
            try:
                min_required = int((task.params or {}).get("min_bars", 0))
            except Exception:
                min_required = 0
            compiled.append(
                CompiledIndicatorTask(
                    name=task.name or "",
                    func=fn,
                    params=task.params or {},
                    min_bars=min_required,
                )
            )
            if min_required > required:
                required = min_required
        with self._lock:
            self.tasks = tasks
            self._compiled_tasks = compiled
            self._required_bars_count = required

    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        # 启动前确保已有任务配置（来自参数或ini）
        if self.tasks:
            self.update_tasks(self.tasks)
        else:
            try:
                self.update_tasks(load_indicator_tasks(self.config_path))
            except Exception:
                pass
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="indicator-worker", daemon=True)
        self._thread.start()
        if self.settings.backfill_enabled:
            self._backfill_thread = threading.Thread(
                target=self._backfill_indicators, name="indicator-backfill", daemon=True
            )
            self._backfill_thread.start()
        return self

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        if self._backfill_thread:
            self._backfill_thread.join(timeout=timeout)

    def _run(self):
        """主循环：等待OHLC事件，合并重复事件后按symbol/tf计算。"""
        while not self._stop.is_set():
            self._maybe_reload_config()
            # 批量处理待计算事件，避免频繁触发计算
            event = self.service.get_ohlc_event(timeout=self.poll_seconds)
            if event is None:
                continue
            # event结构: (symbol, timeframe, bar_time)
            pending: Dict[Tuple[str, str], Optional[datetime]] = {(event[0], event[1]): event[2]}
            # 清空剩余事件，用于合并重复的 symbol/tf 触发
            while True:
                extra = self.service.get_ohlc_event(timeout=0)
                if extra is None:
                    break
                key = (extra[0], extra[1])
                prev_time = pending.get(key)
                extra_time = extra[2]
                if prev_time is None or (extra_time is not None and extra_time > prev_time):
                    pending[key] = extra_time
            for (symbol, tf), event_time in pending.items():
                # 如果事件时间没有超过已看到的时间，则跳过读取缓存
                if event_time is not None:
                    with self._lock:
                        last_seen = self._last_seen_time.get(ohlc_key(symbol, tf))
                    if last_seen is not None and event_time <= last_seen:
                        continue
                try:
                    self._compute(symbol, tf)
                except Exception:
                    continue

    def _compute(self, symbol: str, timeframe: str):
        """按滑动窗口对新增收盘K线计算指标，并同步缓存/入库。"""
        with self._lock:
            tasks_snapshot = list(self._compiled_tasks)
            required = self._required_bars_count
        if not tasks_snapshot:
            return
        window, new_bars = self._update_local_cache(symbol, timeframe, required)
        if not new_bars:
            return
        # 维护滑动窗口，只对新增的收盘K线计算指标
        key = ohlc_key(symbol, timeframe)
        last_ts = self._last_computed_time.get(key)
        latest_ts: Optional[datetime] = None
        expected_gap = timeframe_seconds(timeframe)
        for bar in new_bars:
            # 连续性判断：出现缺口则重置窗口并跳过该K线的指标更新
            last_in_window = window[-1] if window else None
            if last_in_window is not None:
                delta = (bar.time - last_in_window.time).total_seconds()
                if delta > expected_gap:
                    window.clear()
                    window.append(bar)
                    continue
                if delta <= 0:
                    continue
            window.append(bar)
            if last_ts is not None and bar.time <= last_ts:
                continue
            window_list = list(window)
            data: Dict[str, Any] = {}
            for task in tasks_snapshot:
                if task.min_bars and len(window_list) < task.min_bars:
                    continue
                # 以当前窗口执行指标函数
                try:
                    res = task.func(window_list, task.params)
                    if not isinstance(res, dict):
                        continue
                    if len(res) == 1:
                        only_key, only_val = next(iter(res.items()))
                        task_key = task.name or only_key
                        data[task_key] = only_val
                    else:
                        for k, v in res.items():
                            data[k] = v
                except Exception:
                    continue

            if not data:
                continue
            # 先更新服务缓存，再入队数据库指标写入
            snap = IndicatorSnapshot(symbol, timeframe, dict(data))
            with self._lock:
                self._snapshots[key] = snap
            # 将指标合并到服务端缓存（供API读取）
            self.service.update_ohlc_indicators(symbol, timeframe, bar.time, dict(data))
            if self.storage:
                try:
                    row = (
                        bar.symbol,
                        bar.timeframe,
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.volume,
                        bar.time.isoformat(),
                        dict(data),
                    )
                    self.storage.enqueue("ohlc_indicators", row)
                except Exception:
                    pass
            latest_ts = bar.time

        if latest_ts is not None:
            self._last_computed_time[key] = latest_ts

    def get_snapshot(self, symbol: str, timeframe: str) -> Optional[IndicatorSnapshot]:
        key = ohlc_key(symbol, timeframe)
        with self._lock:
            return self._snapshots.get(key)

    def stats(self) -> dict:
        """返回基础运行状态，便于监控。"""
        return {
            "poll_seconds": self.poll_seconds,
            "reload_interval": self.reload_interval,
            "ohlc_cache_limit": self.ohlc_cache_limit,
            "symbols": list(self.symbols),
            "timeframes": list(self.timeframes),
            "tasks": len(self.tasks),
            "config_path": self.config_path,
            "thread_alive": self._thread.is_alive() if self._thread else False,
        }

    # --- 配置热重载 ---
    def _maybe_reload_config(self):
        """根据mtime判断配置是否变更，并刷新任务列表。"""
        if not self.config_path:
            return
        now = time.time()
        if now - self._last_reload_ts < self.reload_interval:
            return
        self._last_reload_ts = now
        try:
            stat = os.stat(self.config_path)
        except FileNotFoundError:
            return
        # 通过检测修改时间判断是否变更
        if self._last_config_mtime is not None and stat.st_mtime <= self._last_config_mtime:
            return
        # 记录当前文件配置修改时间,便于判断是否是有修改过文件从而进行热加载
        self._last_config_mtime = stat.st_mtime
        try:
            # 配置仅支持 ini，按节加载任务
            tasks = load_indicator_tasks(self.config_path)
            self.update_tasks(tasks)
        except Exception:
            return

    def _get_func(self, func_path: str) -> Optional[Callable]:
        """动态导入指标函数并缓存。"""
        if func_path in self._func_cache:
            return self._func_cache[func_path]
        try:
            module_path, fn_name = func_path.rsplit(".", 1)
            module = importlib.import_module(module_path)
            fn = getattr(module, fn_name, None)
            if fn:
                self._func_cache[func_path] = fn
            return fn
        except Exception:
            return None

    # 更新本地缓存并返回当前可用的闭合K线列表
    def _update_local_cache(self, symbol: str, timeframe: str, required: int) -> Tuple[deque, List[OHLC]]:
        """只从服务缓存拉取已闭合K线，并维护本地滑动窗口。"""
        key = ohlc_key(symbol, timeframe)
        # 根据task需求确定缓存长度
        maxlen = max(required, 1)
        cache = self._local_cache.get(key)
        # 初始化cache或调整长度
        if cache is None or cache.maxlen != maxlen:
            cache = deque(maxlen=maxlen)
            self._local_cache[key] = cache
            self._last_seen_time.pop(key, None)
        # 从 MarketDataService 获取最新的闭合K线
        source_bars = self.service.get_ohlc_closed(symbol, timeframe, limit=maxlen)
        source_bars = source_bars or []
        if not source_bars:
            return cache, []
        last_seen = self._last_seen_time.get(key)
        if last_seen is None:
            # 冷启动：预热窗口以满足 min_bars 要求
            warmup_prefix = self._load_warmup_prefix(symbol, timeframe, source_bars, required)
            for bar in warmup_prefix:
                cache.append(bar)
            new_bars = sorted(list(source_bars), key=lambda b: b.time)
            if new_bars:
                self._last_seen_time[key] = new_bars[-1].time
            return cache, new_bars

        new_bars = [bar for bar in source_bars if bar.time > last_seen]
        new_bars = sorted(new_bars, key=lambda b: b.time)
        if new_bars:
            self._last_seen_time[key] = new_bars[-1].time
        return cache, new_bars

    def _load_warmup_prefix(
        self, symbol: str, timeframe: str, source_bars: List[OHLC], required: int
    ) -> List[OHLC]:
        """冷启动补齐窗口：向MT5请求历史K线，仅用于计算，不回写服务。"""
        if required <= 1 or not source_bars:
            return []
        missing = required - 1
        earliest = source_bars[0].time
        fetch_limit = missing + len(source_bars) + 1
        prefix: List[OHLC] = []
        for _ in range(3):
            try:
                history = self.service.client.get_ohlc(symbol, timeframe, fetch_limit)
            except Exception:
                return prefix
            if not history:
                return prefix
            history = sorted(history, key=lambda b: b.time)
            prefix = [bar for bar in history if bar.time < earliest]
            if len(prefix) >= missing:
                return prefix[-missing:]
            fetch_limit += missing
        return prefix[-missing:] if prefix else []

    def _backfill_indicators(self) -> None:
        """从DB回补历史指标，避免历史数据缺指标。"""
        with self._lock:
            tasks_snapshot = list(self._compiled_tasks)
            required = self._required_bars_count
        if not tasks_snapshot:
            return
        db_settings = load_db_settings()
        writer = TimescaleWriter(db_settings)
        for symbol in self.symbols:
            for tf in self.timeframes:
                # 按时间顺序回补DB，用于计算历史指标
                self._backfill_for_symbol_tf(writer, symbol, tf, tasks_snapshot, max(required, 1))

    def _backfill_for_symbol_tf(
        self,
        writer: TimescaleWriter,
        symbol: str,
        timeframe: str,
        tasks: List[CompiledIndicatorTask],
        required: int,
    ) -> None:
        """按时间顺序遍历DB，逐条补齐指标并回写。"""
        last_time: Optional[datetime] = None
        window = deque(maxlen=required)
        batch_size = self.settings.backfill_batch_size
        while not self._stop.is_set():
            rows = writer.fetch_ohlc(symbol, timeframe, last_time, batch_size)
            if not rows:
                return
            for row in rows:
                bar = self._row_to_ohlc(row)
                if bar is None:
                    continue
                window.append(bar)
                last_time = bar.time
                computed = self._compute_indicators_for_window(window, tasks)
                if not computed:
                    continue
                if not self._should_update_indicators(bar.indicators, computed):
                    continue
                self.service.update_ohlc_indicators(symbol, timeframe, bar.time, computed)
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
                            computed,
                        )
                    ],
                    upsert=True,
                )
            if len(rows) < batch_size:
                return

    def _compute_indicators_for_window(
        self, window: deque, tasks: List[CompiledIndicatorTask]
    ) -> Dict[str, float]:
        """在给定窗口上计算所有指标，用于历史回补。"""
        data: Dict[str, float] = {}
        window_list = list(window)
        for task in tasks:
            if task.min_bars and len(window_list) < task.min_bars:
                continue
            try:
                res = task.func(window_list, task.params)
                if not isinstance(res, dict):
                    continue
                if len(res) == 1:
                    only_key, only_val = next(iter(res.items()))
                    task_key = task.name or only_key
                    data[task_key] = only_val
                else:
                    for k, v in res.items():
                        data[k] = v
            except Exception:
                continue
        return data

    def _should_update_indicators(self, existing: Optional[dict], computed: Dict[str, float]) -> bool:
        """判断是否需要写回指标（避免重复覆盖已有结果）。"""
        if not computed:
            return False
        if not existing:
            return True
        for key in computed:
            if key not in existing:
                return True
        return False

    def _row_to_ohlc(self, row: tuple) -> Optional[OHLC]:
        """将DB返回的行转换为OHLC对象（兼容JSON字符串）。"""
        if not row or len(row) < 8:
            return None
        indicators = {}
        if len(row) > 8 and row[8] is not None:
            if isinstance(row[8], dict):
                indicators = row[8]
            else:
                try:
                    indicators = json.loads(row[8])
                except Exception:
                    indicators = {}
        return OHLC(
            symbol=row[0],
            timeframe=row[1],
            open=row[2],
            high=row[3],
            low=row[4],
            close=row[5],
            volume=row[6],
            time=row[7],
            indicators=indicators,
        )
