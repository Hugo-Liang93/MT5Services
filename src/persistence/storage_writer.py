from __future__ import annotations

import logging
import queue
from collections import deque
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional

from src.config import StorageSettings, load_storage_settings
from src.config.utils import load_ini_config, resolve_config_path
from src.persistence.db import TimescaleWriter

logger = logging.getLogger(__name__)


class StorageWriter:
    """
    通用写入服务：维护可配置的队列和写线程，支持行情与指标入库。
    上游模块只需 enqueue，落库策略（批量/重试/节流）在此统一处理。
    """

    def __init__(
        self,
        db_writer: Optional[TimescaleWriter] = None,
        config_path: Optional[str] = None,
        storage_settings: Optional[StorageSettings] = None,
    ):
        self.settings = storage_settings or load_storage_settings()
        self.db = db_writer or TimescaleWriter()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.config_path = resolve_config_path(config_path or "storage.ini")

        # 动态管理的通道：name -> {queue, pending(deque), flush_interval, batch_size, write_fn, enabled_fn}
        self._channels: Dict[str, Dict[str, object]] = {}
        self._last_flush: Dict[str, float] = {}
        if not self._register_channels_from_config():
            raise RuntimeError("No storage channels configured; please provide config/storage.ini")

    # --- 生命周期 ---
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.db.init_schema()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="storage-writer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        # 强制 flush 所有通道
        for name in list(self._channels.keys()):
            self._maybe_flush(name, self._channels[name], force=True)
        if self._thread:
            self._thread.join(timeout=timeout)

    # --- enqueue API ---
    def enqueue(self, channel: str, item: tuple) -> None:
        """通用入队接口，便于外部自定义通道使用。"""
        self._enqueue(channel, item)

    # --- 内部线程 ---
    def _run(self) -> None:
        while not self._stop.is_set() or self._has_pending():
            for name, ch in self._channels.items():
                self._drain_queue(ch["queue"], ch["pending"], ch["batch_size"])
                self._maybe_flush(name, ch)
            self._stop.wait(0.1)

    def _has_pending(self) -> bool:
        for ch in self._channels.values():
            if not ch["queue"].empty() or ch["pending"]:
                return True
        return False

    def _enqueue(self, name: str, item: tuple) -> None:
        ch = self._channels.get(name)
        if not ch:
            logger.warning("Channel %s not registered, dropping data", name)
            return
        try:
            ch["queue"].put_nowait(item)
        except queue.Full:
            logger.warning("%s queue full, dropping data", name)

    def _drain_queue(self, q: queue.Queue, pending: deque, batch_size: int) -> None:
        if pending.maxlen is not None and len(pending) >= pending.maxlen:
            return
        space = pending.maxlen - len(pending) if pending.maxlen else batch_size
        take = min(batch_size, space)
        for _ in range(take):
            try:
                pending.append(q.get_nowait())
            except queue.Empty:
                break

    def _maybe_flush(self, name: str, ch: Dict[str, object], force: bool = False) -> None:
        pending: deque = ch["pending"]  # type: ignore
        if not pending or not ch["enabled_fn"]():  # type: ignore
            return
        now = time.time()
        last = self._last_flush.get(name, 0)
        interval = ch["flush_interval"]  # type: ignore
        batch_size = ch["batch_size"]  # type: ignore
        due_time = (now - last) >= interval
        due_batch = len(pending) >= batch_size
        if force or due_time or due_batch:
            attempts = 0
            while attempts < self.settings.flush_retry_attempts:
                try:
                    ch["write_fn"](list(pending))  # type: ignore
                    pending.clear()
                    self._last_flush[name] = now
                    return
                except Exception as exc:  # pragma: no cover
                    attempts += 1
                    logger.warning("Flush %s failed (attempt %s): %s", name, attempts, exc)
                    if attempts >= self.settings.flush_retry_attempts:
                        self._last_flush[name] = now
                        return
                    time.sleep(self.settings.flush_retry_backoff)

    def register_channel(
        self,
        name: str,
        maxsize: int,
        flush_interval: float,
        batch_size: int,
        write_fn: Callable[[Iterable[tuple]], None],
        enabled: Optional[Callable[[], bool]] = None,
        pending_maxsize: Optional[int] = None,
    ) -> None:
        enabled_fn = enabled or (lambda: True)
        self._channels[name] = {
            "queue": queue.Queue(maxsize=maxsize),
            "pending": deque(maxlen=pending_maxsize or maxsize * 2),
            "flush_interval": flush_interval,
            "batch_size": batch_size,
            "write_fn": write_fn,
            "enabled_fn": enabled_fn,
        }
        self._last_flush[name] = time.time()

    # --- 监控 ---
    def stats(self) -> dict:
        queues = {}
        for name, ch in self._channels.items():
            queues[name] = {
                "size": ch["queue"].qsize(),
                "max": ch["queue"].maxsize,
                "pending": len(ch["pending"]),
            }
        return {
            "queues": queues,
            "threads": {"writer_alive": self._thread.is_alive() if self._thread else False},
        }

    # --- config helpers ---
    def _bool(self, value: str, default: bool) -> bool:
        if value is None:
            return default
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
        return default

    def _register_channels_from_config(self) -> bool:
        """
        从配置文件注册通道；参数必须在 ini 中提供，不使用代码内置默认值。
        字段：type,maxsize,flush_interval,batch_size,enabled(optional)
        """
        cfg_target = self.config_path or "storage.ini"
        cfg_path, parser = load_ini_config(cfg_target)
        if not parser:
            return False
        self.config_path = cfg_path

        registered = False
        for section in parser.sections():
            ch_type = parser.get(section, "type", fallback=section).strip().lower()
            maxsize = parser.getint(section, "maxsize", fallback=None)
            flush_interval = parser.getfloat(section, "flush_interval", fallback=None)
            batch_size = parser.getint(section, "batch_size", fallback=None)
            enabled_val = parser.get(section, "enabled", fallback=None)
            if maxsize is None or flush_interval is None or batch_size is None:
                logger.warning("Channel %s missing required params, skipping", section)
                continue

            write_fn = self._resolve_write_fn(ch_type)
            enabled_fn = self._resolve_enabled_fn(ch_type, enabled_val)
            if write_fn is None:
                logger.warning("Unknown channel type %s in %s, skipping", ch_type, section)
                continue

            self.register_channel(
                name=section,
                maxsize=maxsize,
                flush_interval=flush_interval,
                batch_size=batch_size,
                write_fn=write_fn,
                enabled=enabled_fn,
            )
            registered = True

        if not registered:
            logger.warning("No channels registered from %s", self.config_path)
        else:
            logger.info("Registered %s channels from %s", len(self._channels), self.config_path)
        return registered

    # 当有新的通道类型时，在此添加映射
    def _resolve_write_fn(self, ch_type: str):
        mapping = {
            "ticks": self.db.write_ticks,
            "quotes": self.db.write_quotes,
            "intrabar": self.db.write_ohlc_intrabar,
            "ohlc": lambda rows: self.db.write_ohlc(rows, upsert=self.settings.ohlc_upsert_open_bar),
            "indicators": self.db.write_indicators,
        }
        return mapping.get(ch_type)

    def _resolve_enabled_fn(self, ch_type: str, enabled_val: Optional[str]):
        flag = self._bool(enabled_val, True)
        defaults = {
            "quotes": lambda: self.settings.quote_flush_enabled,
            "intrabar": lambda: self.settings.intrabar_enabled,
        }
        base_fn = defaults.get(ch_type, lambda: True)
        return lambda: flag and base_fn()
