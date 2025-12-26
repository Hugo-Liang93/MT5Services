"""
指标计算后台线程（配置驱动）：
- 任务列表来自配置文件（ini/JSON），支持定时热加载。
- 统一约定：指标函数签名为 fn(bars, params) -> dict 或标量。
- 与 ingestion 解耦：只读 MarketDataService 缓存。
"""

from __future__ import annotations

import configparser
import importlib
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from src.core.market_service import MarketDataService
from src.persistence.storage_writer import StorageWriter


@dataclass
class IndicatorTask:
    """指标任务定义。"""

    name: str           # 指标名称/配置节名称
    func_path: str      # 计算函数全路径，如 src.indicators.adapters.sma
    params: Dict[str, Any]


class IndicatorSnapshot:
    """单个 symbol+timeframe 的指标快照。"""

    def __init__(self, symbol: str, timeframe: str, data: dict):
        self.symbol = symbol
        self.timeframe = timeframe
        self.data = data  # 指标键值对


class IndicatorWorker:
    """
    可配置的指标计算后台线程。
    """

    def __init__(
        self,
        service: MarketDataService,
        symbols: List[str],
        timeframes: List[str],
        tasks: List[IndicatorTask],
        poll_seconds: float = 5.0,
        ohlc_limit: int = 500,
        config_path: Optional[str] = None,
        reload_interval: float = 60.0,
        storage: Optional[StorageWriter] = None,
    ):
        self.service = service
        self.symbols = symbols
        self.timeframes = timeframes
        self.tasks = tasks
        self.poll_seconds = poll_seconds
        self.ohlc_limit = ohlc_limit
        self.config_path = self._resolve_config_path(config_path)
        self.reload_interval = reload_interval
        self.storage = storage

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._snapshots: Dict[str, IndicatorSnapshot] = {}
        self._last_config_mtime: Optional[float] = None
        self._last_reload_ts: float = 0.0
        self._func_cache: Dict[str, Callable] = {}

    def update_tasks(self, tasks: List[IndicatorTask]) -> None:
        with self._lock:
            self.tasks = tasks

    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="indicator-worker", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self):
        while not self._stop.is_set():
            start = time.time()
            self._maybe_reload_config()
            for symbol in self.symbols:
                for tf in self.timeframes:
                    try:
                        self._compute(symbol, tf)
                    except Exception:
                        continue
            elapsed = time.time() - start
            self._stop.wait(max(0, self.poll_seconds - elapsed))

    def _compute(self, symbol: str, timeframe: str):
        bars = self.service.get_ohlc(symbol, timeframe, limit=self.ohlc_limit)
        if not bars:
            return

        data: Dict[str, Any] = {}
        with self._lock:
            tasks_snapshot = list(self.tasks)

        for task in tasks_snapshot:
            fn = self._get_func(task.func_path)
            if fn is None:
                continue
            try:
                res = fn(bars, task.params)
                if not isinstance(res, dict):
                    # 强制要求返回 dict，非 dict 直接跳过
                    continue
                if len(res) == 1:
                    # 单输出默认用节名/任务名作 key，更直观
                    only_key, only_val = next(iter(res.items()))
                    key = task.name or only_key
                    data[key] = only_val
                else:
                    for k, v in res.items():
                        data[k] = v
            except Exception:
                continue

        key = f"{symbol}:{timeframe}"
        snap = IndicatorSnapshot(symbol, timeframe, data)
        with self._lock:
            self._snapshots[key] = snap

        if self.storage and data:
            # 将指标合并写回 ohlc（JSONB 列），上游 ingestion 可继续写基础 bar，worker 负责 upsert 指标字段
            try:
                last_bar = bars[-1]
                row = (
                    last_bar.symbol,
                    last_bar.timeframe,
                    last_bar.open,
                    last_bar.high,
                    last_bar.low,
                    last_bar.close,
                    last_bar.volume,
                    last_bar.time.isoformat(),
                    data,
                )
                self.storage.enqueue("ohlc", row)
            except Exception:
                pass

    def get_snapshot(self, symbol: str, timeframe: str) -> Optional[IndicatorSnapshot]:
        key = f"{symbol}:{timeframe}"
        with self._lock:
            return self._snapshots.get(key)

    def _resolve_config_path(self, config_path: Optional[str]) -> Optional[str]:
        if config_path:
            return os.path.abspath(config_path)
        default_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "config", "indicators.ini")
        )
        return default_path if os.path.exists(default_path) else None

    # --- 配置热重载 ---
    def _maybe_reload_config(self):
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
        if self._last_config_mtime is not None and stat.st_mtime <= self._last_config_mtime:
            return
        self._last_config_mtime = stat.st_mtime
        try:
            # 配置仅支持 ini，按节加载任务
            tasks = self._load_tasks_from_ini(self.config_path)
            self.update_tasks(tasks)
        except Exception:
            return

    def _get_func(self, func_path: str) -> Optional[Callable]:
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

    def _load_tasks_from_ini(self, path: str) -> List[IndicatorTask]:
        parser = configparser.ConfigParser()
        parser.read(path, encoding="utf-8")
        tasks: List[IndicatorTask] = []
        for section in parser.sections():
            func_path = parser.get(section, "func", fallback=None)
            if not func_path:
                continue
            params_raw = parser.get(section, "params", fallback="{}")
            try:
                params = json.loads(params_raw)
            except Exception:
                params = {}
            tasks.append(
                IndicatorTask(
                    name=section,
                    func_path=func_path,
                    params=params,
                )
            )
        return tasks
