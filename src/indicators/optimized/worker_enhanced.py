"""
增强的指标计算工作器
- 增加缓存一致性检查
- 集成本地事件存储
- 改进错误处理和重试机制
"""

from __future__ import annotations

import importlib
import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.core.market_service import MarketDataService
from src.utils.common import ohlc_key, timeframe_seconds
from src.config import IndicatorSettings, load_indicator_settings, load_indicator_tasks, load_db_settings
from src.indicators.types import IndicatorTask
from src.clients.mt5_market import OHLC
from src.persistence.db import TimescaleWriter
from src.persistence.storage_writer import StorageWriter
from src.utils.event_store import get_event_store

import logging
logger = logging.getLogger(__name__)


class EnhancedIndicatorSnapshot:
    """增强的指标快照，包含更多元数据"""
    
    def __init__(self, symbol: str, timeframe: str, data: dict, timestamp: datetime):
        self.symbol = symbol
        self.timeframe = timeframe
        self.data = data  # 指标键值对
        self.timestamp = timestamp  # 计算时间
        self.bar_time = None  # 对应的K线时间
        self.cache_hit = False  # 是否来自缓存


@dataclass
class CompiledIndicatorTask:
    name: str
    func: Callable
    params: Dict[str, Any]
    min_bars: int


class EnhancedIndicatorWorker:
    """
    增强的指标计算工作器
    
    改进点：
    1. 使用本地事件存储确保事件不丢失
    2. 定期检查缓存一致性
    3. 改进错误处理和重试机制
    4. 增加性能监控
    """
    
    def __init__(
        self,
        service: MarketDataService,
        symbols: List[str],
        timeframes: List[str],
        tasks: List[IndicatorTask],
        indicator_settings: Optional[IndicatorSettings] = None,
        storage: Optional[StorageWriter] = None,
        event_store_db_path: str = "events.db"
    ):
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
        
        # 事件存储
        self.event_store = get_event_store(event_store_db_path)
        
        # 线程控制
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._backfill_thread: Optional[threading.Thread] = None
        self._consistency_thread: Optional[threading.Thread] = None
        
        # 共享状态锁
        self._lock = threading.Lock()
        
        # 指标快照
        self._snapshots: Dict[str, EnhancedIndicatorSnapshot] = {}
        
        # 配置热加载
        self._last_config_mtime: Optional[float] = None
        self._last_reload_ts: float = 0.0
        
        # 指标函数缓存
        self._func_cache: Dict[str, Callable] = {}
        
        # 时间指针
        self._last_computed_time: Dict[str, datetime] = {}
        self._last_seen_time: Dict[str, datetime] = {}
        
        # 本地滑动窗口缓存
        self._local_cache: Dict[str, deque] = {}
        
        # 编译后的任务
        self._compiled_tasks: List[CompiledIndicatorTask] = []
        self._required_bars_count: int = 0
        
        # 性能监控
        self._performance_stats: Dict[str, Any] = {
            "total_computations": 0,
            "failed_computations": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "avg_computation_time_ms": 0,
            "last_computation_time": None,
            "consistency_checks": 0,
            "consistency_fixes": 0
        }
        
        # 一致性检查间隔（秒）
        self._consistency_check_interval = 300  # 5分钟
        self._last_consistency_check = 0.0
        
        logger.info("EnhancedIndicatorWorker initialized")

    def update_tasks(self, tasks: List[IndicatorTask]) -> None:
        """编译任务配置并统计所需最小窗口长度"""
        compiled: List[CompiledIndicatorTask] = []
        required = 0
        
        for task in tasks:
            fn = self._get_func(task.func_path)
            if fn is None:
                logger.warning(f"Failed to load indicator function: {task.func_path}")
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
        
        logger.info(f"Updated tasks: {len(compiled)} tasks, required bars: {required}")

    def start(self):
        """启动工作器"""
        if self._thread and self._thread.is_alive():
            logger.warning("Worker already running")
            return self
        
        # 启动前确保已有任务配置
        if self.tasks:
            self.update_tasks(self.tasks)
        else:
            try:
                tasks = load_indicator_tasks(self.config_path)
                self.update_tasks(tasks)
            except Exception as e:
                logger.error(f"Failed to load tasks from config: {e}")
        
        self._stop.clear()
        
        # 主计算线程
        self._thread = threading.Thread(
            target=self._run, 
            name="enhanced-indicator-worker", 
            daemon=True
        )
        self._thread.start()
        
        # 一致性检查线程
        self._consistency_thread = threading.Thread(
            target=self._consistency_check_loop,
            name="indicator-consistency-check",
            daemon=True
        )
        self._consistency_thread.start()
        
        # 历史回补线程
        if self.settings.backfill_enabled:
            self._backfill_thread = threading.Thread(
                target=self._backfill_indicators,
                name="enhanced-indicator-backfill",
                daemon=True
            )
            self._backfill_thread.start()
        
        logger.info("EnhancedIndicatorWorker started")
        return self

    def stop(self, timeout: float = 5.0):
        """停止工作器"""
        self._stop.set()
        
        if self._thread:
            self._thread.join(timeout=timeout)
        
        if self._consistency_thread:
            self._consistency_thread.join(timeout=timeout)
        
        if self._backfill_thread:
            self._backfill_thread.join(timeout=timeout)
        
        logger.info("EnhancedIndicatorWorker stopped")

    def _run(self):
        """主循环：从事件存储获取事件并计算指标"""
        logger.info("Indicator worker main loop started")
        
        while not self._stop.is_set():
            try:
                self._maybe_reload_config()
                
                # 从事件存储获取下一个事件
                event = self.event_store.get_next_event()
                if event is None:
                    # 没有事件，等待一段时间
                    self._stop.wait(self.poll_seconds)
                    continue
                
                symbol, timeframe, event_time = event
                
                # 处理事件
                start_time = time.time()
                success = self._process_event(symbol, timeframe, event_time)
                computation_time = (time.time() - start_time) * 1000  # 毫秒
                
                # 更新性能统计
                with self._lock:
                    self._performance_stats["total_computations"] += 1
                    if not success:
                        self._performance_stats["failed_computations"] += 1
                    
                    # 更新平均计算时间（指数移动平均）
                    old_avg = self._performance_stats["avg_computation_time_ms"]
                    if old_avg == 0:
                        self._performance_stats["avg_computation_time_ms"] = computation_time
                    else:
                        self._performance_stats["avg_computation_time_ms"] = 0.9 * old_avg + 0.1 * computation_time
                    
                    self._performance_stats["last_computation_time"] = datetime.utcnow()
                
                # 标记事件状态
                if success:
                    self.event_store.mark_event_completed(symbol, timeframe, event_time)
                else:
                    error_msg = f"Failed to compute indicators for {symbol}/{timeframe} at {event_time}"
                    self.event_store.mark_event_failed(symbol, timeframe, event_time, error_msg)
                
            except Exception as e:
                logger.exception(f"Unexpected error in indicator worker main loop: {e}")
                time.sleep(1)  # 避免快速循环

    def _process_event(self, symbol: str, timeframe: str, event_time: datetime) -> bool:
        """
        处理单个事件
        
        Returns:
            bool: 是否成功处理
        """
        try:
            # 检查事件时间是否合理（不能是未来时间）
            if event_time > datetime.utcnow():
                logger.warning(f"Future event time: {symbol}/{timeframe} at {event_time}")
                return False
            
            # 检查是否需要计算
            key = ohlc_key(symbol, timeframe)
            last_computed = self._last_computed_time.get(key)
            
            if last_computed is not None and event_time <= last_computed:
                # 已经计算过这个时间的指标
                with self._lock:
                    self._performance_stats["cache_hits"] += 1
                logger.debug(f"Event already computed: {symbol}/{timeframe} at {event_time}")
                return True
            
            # 计算指标
            result = self._compute_for_event(symbol, timeframe, event_time)
            
            if result:
                # 更新最后计算时间
                self._last_computed_time[key] = event_time
                with self._lock:
                    self._performance_stats["cache_misses"] += 1
                
                logger.info(f"Successfully computed indicators for {symbol}/{timeframe} at {event_time}")
                return True
            else:
                logger.warning(f"Failed to compute indicators for {symbol}/{timeframe} at {event_time}")
                return False
                
        except Exception as e:
            logger.exception(f"Error processing event {symbol}/{timeframe} at {event_time}: {e}")
            return False

    def _compute_for_event(self, symbol: str, timeframe: str, event_time: datetime) -> bool:
        """为特定事件计算指标"""
        with self._lock:
            tasks_snapshot = list(self._compiled_tasks)
            required = self._required_bars_count
        
        if not tasks_snapshot:
            logger.warning(f"No tasks configured for {symbol}/{timeframe}")
            return False
        
        # 更新本地缓存
        window, new_bars = self._update_local_cache(symbol, timeframe, required)
        if not new_bars:
            logger.debug(f"No new bars for {symbol}/{timeframe}")
            return False
        
        # 找到对应event_time的bar
        target_bar = None
        for bar in new_bars:
            if bar.time == event_time:
                target_bar = bar
                break
        
        if target_bar is None:
            logger.warning(f"Event time {event_time} not found in new bars for {symbol}/{timeframe}")
            return False
        
        # 维护滑动窗口
        key = ohlc_key(symbol, timeframe)
        expected_gap = timeframe_seconds(timeframe)
        
        # 检查连续性
        last_in_window = window[-1] if window else None
        if last_in_window is not None:
            delta = (target_bar.time - last_in_window.time).total_seconds()
            if delta > expected_gap:
                logger.warning(f"Gap detected in {key}: {delta} seconds, resetting window")
                window.clear()
                window.append(target_bar)
                return False
            if delta <= 0:
                logger.warning(f"Non-increasing time in {key}: {target_bar.time} <= {last_in_window.time}")
                return False
        
        window.append(target_bar)
        
        # 计算指标
        window_list = list(window)
        data: Dict[str, Any] = {}
        
        for task in tasks_snapshot:
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
                        
            except Exception as e:
                logger.error(f"Error computing task {task.name} for {symbol}/{timeframe}: {e}")
                continue
        
        if not data:
            logger.warning(f"No indicators computed for {symbol}/{timeframe} at {event_time}")
            return False
        
        # 更新服务缓存
        snap = EnhancedIndicatorSnapshot(symbol, timeframe, dict(data), datetime.utcnow())
        snap.bar_time = target_bar.time
        
        with self._lock:
            self._snapshots[key] = snap
        
        # 更新服务端缓存
        self.service.update_ohlc_indicators(symbol, timeframe, target_bar.time, dict(data))
        
        # 写入存储
        if self.storage:
            try:
                row = (
                    target_bar.symbol,
                    target_bar.timeframe,
                    target_bar.open,
                    target_bar.high,
                    target_bar.low,
                    target_bar.close,
                    target_bar.volume,
                    target_bar.time.isoformat(),
                    dict(data),
                )
                self.storage.enqueue("ohlc_indicators", row)
            except Exception as e:
                logger.error(f"Failed to enqueue indicators for storage: {e}")
        
        return True

    def _consistency_check_loop(self):
        """一致性检查循环"""
        logger.info("Consistency check loop started")
        
        while not self._stop.is_set():
            try:
                current_time = time.time()
                if current_time - self._last_consistency_check > self._consistency_check_interval:
                    self._check_consistency()
                    self._last_consistency_check = current_time
                
                # 每30秒检查一次
                self._stop.wait(30)
                
            except Exception as e:
                logger.exception(f"Error in consistency check loop: {e}")
                time.sleep(60)  # 出错后等待更长时间

    def _check_consistency(self):
        """检查并修复缓存一致性"""
        logger.debug("Starting consistency check")
        
        with self._lock:
            self._performance_stats["consistency_checks"] += 1
        
        fixes_applied = 0
        
        for symbol in self.symbols:
            for tf in self.timeframes:
                if self._ensure_cache_consistency(symbol, tf):
                    fixes_applied += 1
        
        if fixes_applied > 0:
            with self._lock:
                self._performance_stats["consistency_fixes"] += fixes_applied
            logger.info(f"Consistency check applied {fixes_applied} fixes")
        else:
            logger.debug("Consistency check completed, no fixes needed")

    def _ensure_cache_consistency(self, symbol: str, timeframe: str) -> bool:
        """确保本地缓存与服务缓存一致"""
        key = ohlc_key(symbol, timeframe)
        
        # 从服务获取最新数据
        service_bars = self.service.get_ohlc_closed(symbol, timeframe, limit=self._required_bars_count)
        if not service_bars:
            return False
        
        service_last = service_bars[-1].time
        local_last = self._last_seen_time.get(key)
        
        # 如果本地缓存落后或为空，重新构建
        if local_last is None or service_last > local_last:
            logger.info(f"Rebuilding local cache for {key}, service_last={service_last}, local_last={local_last}")
            
            # 清空并重建本地缓存
            if key in self._local_cache:
                self._local_cache[key].clear()
            else:
                self._local_cache[key] = deque(maxlen=self._required_bars_count)
            
            # 按时间顺序添加数据
            for bar in sorted(service_bars, key=lambda b: b.time):
                self._local_cache[key].append(bar)
            
            self._last_seen_time[key] = service_last
            
            # 重新计算最新K线的指标
            if len(self._local_cache[key]) >= self._required_bars_count:
                self._recompute_latest_indicators(symbol, timeframe)
            
            return True
        
        return False

    def _recompute_latest_indicators(self, symbol: str, timeframe: str):
        """重新计算最新K线的指标"""
        key = ohlc_key(symbol, timeframe)
        
        if key not in self._local_cache or not self._local_cache[key]:
            return
        
        latest_bar = self._local_cache[key][-1]
        
        # 创建事件并发布到事件存储
        self.event_store.publish_event(symbol, timeframe, latest_bar.time)
        logger.debug(f"Published recomputation event for {symbol}/{timeframe} at {latest_bar.time}")

    def _update_local_cache(self, symbol: str, timeframe: str, required: int) -> Tuple[deque, List[OHLC]]:
        """只从服务缓存拉取已闭合K线，并维护本地滑动窗口"""
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
        
        # 获取新数据
        new_bars = [bar for bar in source_bars if bar.time > last_seen]
        new_bars = sorted(new_bars, key=lambda b: b.time)
        
        if new_bars:
            self._last_seen_time[key] = new_bars[-1].time
        
        return cache, new_bars

    def _load_warmup_prefix(
        self, symbol: str, timeframe: str, source_bars: List[OHLC], required: int
    ) -> List[OHLC]:
        """冷启动补齐窗口：向MT5请求历史K线，仅用于计算，不回写服务"""
        if required <= 1 or not source_bars:
            return []
        
        missing = required - 1
        earliest = source_bars[0].time
        fetch_limit = missing + len(source_bars) + 1
        prefix: List[OHLC] = []
        
        for _ in range(3):  # 最多尝试3次
            try:
                history = self.service.client.get_ohlc(symbol, timeframe, fetch_limit)
            except Exception as e:
                logger.error(f"Failed to fetch warmup data for {symbol}/{timeframe}: {e}")
                return prefix
            
            if not history:
                return prefix
            
            history = sorted(history, key=lambda b: b.time)
            prefix = [bar for bar in history if bar.time < earliest]
            
            if len(prefix) >= missing:
                return prefix[-missing:]
            
            fetch_limit += missing
        
        return prefix[-missing:] if prefix else []

    def _maybe_reload_config(self):
        """根据mtime判断配置是否变更，并刷新任务列表"""
        if not self.config_path:
            return
        
        now = time.time()
        if now - self._last_reload_ts < self.reload_interval:
            return
        
        self._last_reload_ts = now
        
        try:
            stat = os.stat(self.config_path)
        except FileNotFoundError:
            logger.warning(f"Config file not found: {self.config_path}")
            return
        
        # 通过检测修改时间判断是否变更
        if self._last_config_mtime is not None and stat.st_mtime <= self._last_config_mtime:
            return
        
        # 记录当前文件配置修改时间
        self._last_config_mtime = stat.st_mtime
        
        try:
            # 配置仅支持 ini，按节加载任务
            tasks = load_indicator_tasks(self.config_path)
            self.update_tasks(tasks)
            logger.info(f"Reloaded config from {self.config_path}, {len(tasks)} tasks")
        except Exception as e:
            logger.error(f"Failed to reload config from {self.config_path}: {e}")

    def _get_func(self, func_path: str) -> Optional[Callable]:
        """动态导入指标函数并缓存"""
        if func_path in self._func_cache:
            return self._func_cache[func_path]
        
        try:
            module_path, fn_name = func_path.rsplit(".", 1)
            module = importlib.import_module(module_path)
            fn = getattr(module, fn_name, None)
            
            if fn:
                self._func_cache[func_path] = fn
                logger.debug(f"Loaded function: {func_path}")
            else:
                logger.warning(f"Function not found: {fn_name} in {module_path}")
            
            return fn
        except Exception as e:
            logger.error(f"Failed to import function {func_path}: {e}")
            return None

    def get_snapshot(self, symbol: str, timeframe: str) -> Optional[EnhancedIndicatorSnapshot]:
        """获取指标快照"""
        key = ohlc_key(symbol, timeframe)
        with self._lock:
            return self._snapshots.get(key)

    def get_performance_stats(self) -> Dict[str, Any]:
        """获取性能统计信息"""
        with self._lock:
            stats = self._performance_stats.copy()
            
            # 添加事件存储统计
            event_stats = self.event_store.get_stats()
            stats["event_store"] = event_stats
            
            # 计算成功率
            total = stats["total_computations"]
            failed = stats["failed_computations"]
            if total > 0:
                stats["success_rate"] = (total - failed) / total
            else:
                stats["success_rate"] = 0
            
            # 缓存命中率
            hits = stats["cache_hits"]
            misses = stats["cache_misses"]
            total_cache = hits + misses
            if total_cache > 0:
                stats["cache_hit_rate"] = hits / total_cache
            else:
                stats["cache_hit_rate"] = 0
            
            return stats

    def stats(self) -> dict:
        """返回基础运行状态，便于监控"""
        base_stats = {
            "poll_seconds": self.poll_seconds,
            "reload_interval": self.reload_interval,
            "ohlc_cache_limit": self.ohlc_cache_limit,
            "symbols": list(self.symbols),
            "timeframes": list(self.timeframes),
            "tasks": len(self.tasks),
            "config_path": self.config_path,
            "thread_alive": self._thread.is_alive() if self._thread else False,
            "consistency_thread_alive": self._consistency_thread.is_alive() if self._consistency_thread else False,
            "backfill_thread_alive": self._backfill_thread.is_alive() if self._backfill_thread else False,
        }
        
        # 合并性能统计
        perf_stats = self.get_performance_stats()
        base_stats.update(perf_stats)
        
        return base_stats

    def _backfill_indicators(self) -> None:
        """从DB回补历史指标，避免历史数据缺指标"""
        logger.info("Starting indicator backfill")
        
        with self._lock:
            tasks_snapshot = list(self._compiled_tasks)
            required = self._required_bars_count
        
        if not tasks_snapshot:
            logger.warning("No tasks configured for backfill")
            return
        
        db_settings = load_db_settings()
        writer = TimescaleWriter(db_settings)
        
        for symbol in self.symbols:
            for tf in self.timeframes:
                if self._stop.is_set():
                    return
                
                logger.info(f"Backfilling indicators for {symbol}/{tf}")
                self._backfill_for_symbol_tf(writer, symbol, tf, tasks_snapshot, max(required, 1))
        
        logger.info("Indicator backfill completed")

    def _backfill_for_symbol_tf(
        self,
        writer: TimescaleWriter,
        symbol: str,
        timeframe: str,
        tasks: List[CompiledIndicatorTask],
        required: int,
    ) -> None:
        """按时间顺序遍历DB，逐条补齐指标并回写"""
        last_time: Optional[datetime] = None
        window = deque(maxlen=required)
        batch_size = self.settings.backfill_batch_size
        
        while not self._stop.is_set():
            rows = writer.fetch_ohlc(symbol, timeframe, last_time, batch_size)
            if not rows:
                return
            
            for row in rows:
                if self._stop.is_set():
                    return
                
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
                
                # 更新服务缓存
                self.service.update_ohlc_indicators(symbol, timeframe, bar.time, computed)
                
                # 写入数据库
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
        """在给定窗口上计算所有指标，用于历史回补"""
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
                        
            except Exception as e:
                logger.error(f"Error computing task {task.name} in backfill: {e}")
                continue
        
        return data

    def _should_update_indicators(self, existing: Optional[dict], computed: Dict[str, float]) -> bool:
        """判断是否需要写回指标（避免重复覆盖已有结果）"""
        if not computed:
            return False
        
        if not existing:
            return True
        
        for key in computed:
            if key not in existing:
                return True
        
        return False

    def _row_to_ohlc(self, row: tuple) -> Optional[OHLC]:
        """将DB返回的行转换为OHLC对象（兼容JSON字符串）"""
        if not row or len(row) < 8:
            return None
        
        indicators = {}
        if len(row) > 8 and row[8] is not None:
            if isinstance(row[8], dict):
                indicators = row[8]
            else:
                try:
                    indicators = json.loads(row[8])
                except Exception as e:
                    logger.warning(f"Failed to parse indicators JSON: {e}")
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

    def trigger_consistency_check(self):
        """手动触发一致性检查"""
        self._check_consistency()
        logger.info("Manual consistency check triggered")

    def reset_failed_events(self) -> int:
        """重置失败事件"""
        reset_count = self.event_store.reset_failed_events()
        logger.info(f"Reset {reset_count} failed events")
        return reset_count

    def cleanup_old_events(self, days_to_keep: int = 7):
        """清理旧事件"""
        self.event_store.cleanup_old_events(days_to_keep)
        logger.info(f"Cleaned up events older than {days_to_keep} days")
