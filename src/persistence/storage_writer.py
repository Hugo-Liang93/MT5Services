from __future__ import annotations

import json
import logging
import os
import queue
from collections import deque
from pathlib import Path
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional

from src.config import StorageSettings
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
        db_writer: TimescaleWriter,
        config_path: Optional[str] = None,
        storage_settings: StorageSettings = None,
    ):
        if storage_settings is None:
            raise ValueError("storage_settings is required")
        self.settings = storage_settings
        self.db = db_writer
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self.config_path = resolve_config_path(config_path or "storage.ini")

        # 动态管理的通道：name -> {queue, pending(deque), flush_interval, batch_size, write_fn, enabled_fn}
        self._channels: Dict[str, Dict[str, object]] = {}
        self._last_flush: Dict[str, float] = {}
        self._channel_stats: Dict[str, Dict[str, int]] = {}
        self._queue_log_state: Dict[str, Dict[str, float]] = {}
        # DLQ 替代方案：跟踪每通道累计丢弃数，当短期内丢弃速率超过阈值时
        # 发出 ERROR 级别告警，便于监控系统检测数据丢失事件。
        self._drop_alert_last_total: Dict[str, int] = {}
        self._drop_alert_last_at: Dict[str, float] = {}
        # 60 秒内丢弃超过此数则触发 ERROR 告警
        self._drop_alert_rate_threshold = 50
        # DLQ 目录：刷写失败的批次持久化到此处，下次启动时重放
        self._dlq_dir = self._init_dlq_dir()
        if not self._register_channels_from_config():
            raise RuntimeError("No storage channels configured; please provide config/storage.ini")

    # --- DLQ（Dead Letter Queue）---
    def _init_dlq_dir(self) -> Path:
        from src.config.centralized import get_runtime_data_path

        dlq_path = Path(get_runtime_data_path("dlq"))
        dlq_path.mkdir(parents=True, exist_ok=True)
        return dlq_path

    def _dump_to_dlq(self, channel: str, rows: list[tuple]) -> None:
        """将刷写失败的批次序列化到 DLQ 文件。"""
        if not rows:
            return
        ts = int(time.time() * 1000)
        path = self._dlq_dir / f"{channel}_{ts}.jsonl"
        try:
            with open(path, "w", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, default=str) + "\n")
            logger.warning(
                "StorageWriter: dumped %d failed rows for channel '%s' to DLQ: %s",
                len(rows), channel, path.name,
            )
        except Exception as exc:
            logger.error("StorageWriter: failed to write DLQ file %s: %s", path, exc)

    def _replay_dlq(self) -> None:
        """启动时扫描 DLQ 目录，将残留的失败批次重新写入对应通道。"""
        if not self._dlq_dir.exists():
            return
        files = sorted(self._dlq_dir.glob("*.jsonl"))
        if not files:
            return
        logger.info("StorageWriter: found %d DLQ files to replay", len(files))
        for path in files:
            channel = path.stem.rsplit("_", 1)[0]
            ch = self._channels.get(channel)
            if not ch:
                logger.warning("StorageWriter: DLQ channel '%s' not registered, removing %s", channel, path.name)
                path.unlink(missing_ok=True)
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    rows = [tuple(json.loads(line)) for line in f if line.strip()]
                if rows:
                    ch["write_fn"](rows)  # type: ignore[index]
                    logger.info("StorageWriter: replayed %d rows from DLQ %s", len(rows), path.name)
                path.unlink(missing_ok=True)
            except Exception as exc:
                logger.error("StorageWriter: DLQ replay failed for %s: %s (will retry next start)", path.name, exc)

    # --- 生命周期 ---
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.db.init_schema()
        self._replay_dlq()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="storage-writer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            thread = self._thread
            thread.join(timeout=timeout)
            self._thread = None
            if thread.is_alive():
                logger.warning("StorageWriter stop timed out; writer thread still alive")
                return
        with self._lock:
            for name in list(self._channels.keys()):
                self._flush_if_due(name, self._channels[name], force=True)

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # --- enqueue API ---
    def enqueue(self, channel: str, item: tuple) -> None:
        """通用入队接口，便于外部自定义通道使用。"""
        self._enqueue(channel, item)

    def write_now(self, channel: str, items: Iterable[tuple]) -> int:
        """Write a batch directly through the registered channel writer."""
        ch = self._channels.get(channel)
        if not ch:
            logger.warning("Channel %s not registered, skipping direct write", channel)
            return 0
        enabled_fn = ch["enabled_fn"]  # type: ignore[assignment]
        if not enabled_fn():
            return 0
        batch = list(items)
        if not batch:
            return 0
        ch["write_fn"](batch)  # type: ignore[index]
        self._last_flush[channel] = time.time()
        return len(batch)

    def get_channel_batch_size(self, channel: str) -> Optional[int]:
        ch = self._channels.get(channel)
        if not ch:
            return None
        return int(ch["batch_size"])

    # --- 内部线程 ---
    def _run(self) -> None:
        while not self._stop.is_set() or self._has_pending():
            with self._lock:
                for name, ch in self._channels.items():
                    self._drain_queue(ch["queue"], ch["pending"], ch["batch_size"])
                    self._flush_if_due(name, ch)
            self._stop.wait(0.1)

    def _has_pending(self) -> bool:
        with self._lock:
            for ch in self._channels.values():
                if not ch["queue"].empty() or ch["pending"]:
                    return True
            return False

    def _enqueue(self, name: str, item: tuple) -> None:
        """将数据加入队列，添加队列监控"""
        ch = self._channels.get(name)
        if not ch:
            logger.warning("Channel %s not registered, dropping data", name)
            return
        
        q = ch["queue"]
        
        # 监控队列使用率
        self._log_queue_pressure(name, q)
        
        try:
            q.put_nowait(item)
        except queue.Full:
            self._handle_queue_full(name, ch, item)
            # 这里可以添加更复杂的处理逻辑，如写入临时文件

    def _drain_queue(self, q: queue.Queue, pending: deque, batch_size: int) -> None:
        """从队列中取出数据到 pending 缓冲区"""
        # 计算可用空间
        if pending.maxlen:
            available = pending.maxlen - len(pending)
            if available <= 0:
                return
            take = min(batch_size, available)
        else:
            take = batch_size
        
        # 批量获取数据
        drained = []
        for _ in range(take):
            try:
                drained.append(q.get_nowait())
            except queue.Empty:
                break
        
        # 批量添加到 pending
        if drained:
            pending.extend(drained)

    def _flush_if_due(self, name: str, ch: Dict[str, object], force: bool = False) -> None:
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
            batch = list(pending)
            attempts = 0
            while attempts < self.settings.flush_retry_attempts:
                try:
                    ch["write_fn"](batch)  # type: ignore
                    pending.clear()
                    self._last_flush[name] = now
                    return
                except Exception as exc:  # pragma: no cover
                    attempts += 1
                    logger.warning("Flush %s failed (attempt %s): %s", name, attempts, exc)
                    if attempts >= self.settings.flush_retry_attempts:
                        self._dump_to_dlq(name, batch)
                        pending.clear()
                        self._last_flush[name] = now
                        return
                    time.sleep(self.settings.flush_retry_backoff)

    def register_channel(
        self,
        name: str,
        channel_type: str,
        maxsize: int,
        flush_interval: float,
        batch_size: int,
        write_fn: Callable[[Iterable[tuple]], None],
        enabled: Optional[Callable[[], bool]] = None,
        pending_maxsize: Optional[int] = None,
        overflow_policy: Optional[str] = None,
    ) -> None:
        enabled_fn = enabled or (lambda: True)
        with self._lock:
            self._channels[name] = {
                "type": channel_type,
                "queue": queue.Queue(maxsize=maxsize),
                "pending": deque(maxlen=pending_maxsize or maxsize * 2),
                "flush_interval": flush_interval,
                "batch_size": batch_size,
                "write_fn": write_fn,
                "enabled_fn": enabled_fn,
                "overflow_policy": (overflow_policy or self.settings.queue_overflow_policy or "auto").strip().lower(),
            }
            self._last_flush[name] = time.time()
            self._channel_stats[name] = {
                "dropped_oldest": 0,
                "dropped_newest": 0,
                "blocked_puts": 0,
                "full_errors": 0,
            }
            self._queue_log_state[name] = {
                "usage_level": 0.0,
                "last_full_warning_at": 0.0,
            }

    # --- 监控 ---
    def stats(self) -> dict:
        queues = {}
        summary = {"total": 0, "high": 0, "critical": 0, "full": 0}
        with self._lock:
            for name, ch in self._channels.items():
                q: queue.Queue = ch["queue"]  # type: ignore[assignment]
                maxsize = q.maxsize
                size = q.qsize()
                utilization = (size / maxsize) if maxsize else 0.0
                status = self._queue_status(utilization, size, maxsize)
                queues[name] = {
                    "size": size,
                    "max": maxsize,
                    "pending": len(ch["pending"]),
                    "overflow_policy": ch["overflow_policy"],
                    "utilization_pct": round(utilization * 100, 1),
                    "status": status,
                    "drops_oldest": self._channel_stats[name]["dropped_oldest"],
                    "drops_newest": self._channel_stats[name]["dropped_newest"],
                    "blocked_puts": self._channel_stats[name]["blocked_puts"],
                    "full_errors": self._channel_stats[name]["full_errors"],
                }
                summary["total"] += 1
                if status == "full":
                    summary["full"] += 1
                elif status == "critical":
                    summary["critical"] += 1
                elif status == "high":
                    summary["high"] += 1
        return {
            "queues": queues,
            "summary": summary,
            "threads": {"writer_alive": self._thread.is_alive() if self._thread else False},
        }

    def _queue_status(self, utilization: float, size: int, maxsize: int) -> str:
        if maxsize and size >= maxsize:
            return "full"
        if utilization >= 0.95:
            return "critical"
        if utilization >= 0.8:
            return "high"
        return "normal"

    def _log_queue_pressure(self, name: str, q: queue.Queue) -> None:
        if not q.maxsize:
            return

        if not hasattr(self, "_queue_log_state"):
            self._queue_log_state = {}
        utilization = q.qsize() / q.maxsize
        state = self._queue_log_state.setdefault(
            name,
            {"usage_level": 0.0, "last_full_warning_at": 0.0},
        )
        previous_level = int(state.get("usage_level", 0))

        if utilization >= 0.95:
            current_level = 2
        elif utilization >= 0.8:
            current_level = 1
        elif utilization < 0.7:
            current_level = 0
        else:
            current_level = previous_level

        state["usage_level"] = float(current_level)
        if current_level == previous_level:
            return

        if current_level == 2:
            logger.warning("Queue %s usage critical: %.1f%%", name, utilization * 100)
        elif current_level == 1:
            logger.warning("Queue %s usage high: %.1f%%", name, utilization * 100)
        elif previous_level > 0:
            logger.info("Queue %s usage recovered: %.1f%%", name, utilization * 100)

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
            if section.strip().lower() == "storage":
                continue
            ch_type = parser.get(section, "type", fallback=section).strip().lower()
            maxsize = parser.getint(section, "maxsize", fallback=None)
            flush_interval = parser.getfloat(section, "flush_interval", fallback=None)
            batch_size = parser.getint(section, "batch_size", fallback=None)
            page_size = parser.getint(section, "page_size", fallback=None)
            enabled_val = parser.get(section, "enabled", fallback=None)
            if maxsize is None or flush_interval is None or batch_size is None or page_size is None:
                logger.warning("Channel %s missing required params, skipping", section)
                continue

            write_fn = self._resolve_write_fn(ch_type, page_size)
            enabled_fn = self._resolve_enabled_fn(ch_type, enabled_val)
            if write_fn is None:
                logger.warning("Unknown channel type %s in %s, skipping", ch_type, section)
                continue

            self.register_channel(
                name=section,
                channel_type=ch_type,
                maxsize=maxsize,
                flush_interval=flush_interval,
                batch_size=batch_size,
                write_fn=write_fn,
                enabled=enabled_fn,
                overflow_policy=parser.get(section, "overflow_policy", fallback=None),
            )
            registered = True

        if not registered:
            logger.warning("No channels registered from %s", self.config_path)
        else:
            logger.info("Registered %s channels from %s", len(self._channels), self.config_path)
        return registered

    # 当有新的通道类型时，在此添加映射
    def _resolve_write_fn(self, ch_type: str, page_size: Optional[int]):
        if page_size is None:
            return None
        mapping = {
            "ticks": lambda rows: self.db.write_ticks(rows, page_size=page_size),
            "quotes": lambda rows: self.db.write_quotes(rows, page_size=page_size),
            "intrabar": lambda rows: self.db.write_ohlc_intrabar(rows, page_size=page_size),
            "ohlc": lambda rows: self.db.write_ohlc(
                rows, upsert=self.settings.ohlc_upsert_open_bar, page_size=page_size
            ),
            "ohlc_indicators": lambda rows: self.db.write_ohlc(rows, upsert=True, page_size=page_size),
            "economic_calendar": lambda rows: self.db.write_economic_calendar(rows, page_size=page_size),
            "economic_calendar_updates": lambda rows: self.db.write_economic_calendar_updates(
                rows, page_size=page_size
            ),
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

    def _handle_queue_full(self, name: str, ch: Dict[str, object], item: tuple) -> None:
        q: queue.Queue = ch["queue"]  # type: ignore[assignment]
        policy = self._resolve_overflow_policy(ch)
        if not hasattr(self, "_queue_log_state"):
            self._queue_log_state = {}
        log_state = self._queue_log_state.setdefault(
            name,
            {"usage_level": 0.0, "last_full_warning_at": 0.0},
        )
        if policy == "block":
            if self._thread is None or not self._thread.is_alive():
                self._channel_stats[name]["dropped_newest"] += 1
                self._channel_stats[name]["full_errors"] += 1
                logger.error(
                    "Queue %s is full and storage writer is not running, dropped newest item: %s",
                    name,
                    item,
                )
                return

            timeout = max(0.1, self.settings.queue_put_timeout)
            warned = False
            while not self._stop.is_set():
                try:
                    q.put(item, timeout=timeout)
                    self._channel_stats[name]["blocked_puts"] += 1
                    log_state["last_full_warning_at"] = 0.0
                    if warned:
                        logger.warning("Queue %s recovered after blocking producer writes", name)
                    return
                except queue.Full:
                    now = time.monotonic()
                    if now - log_state.get("last_full_warning_at", 0.0) >= 5.0:
                        warned = True
                        log_state["last_full_warning_at"] = now
                        logger.warning(
                            "Queue %s remains full after %.2fs; blocking producer to avoid data loss",
                            name,
                            timeout,
                        )

            self._channel_stats[name]["dropped_newest"] += 1
            self._channel_stats[name]["full_errors"] += 1
            logger.error("Queue %s stopped while blocking, dropped newest item: %s", name, item)
            return

        if policy == "drop_oldest":
            try:
                dropped = q.get_nowait()
                q.put_nowait(item)
                self._channel_stats[name]["dropped_oldest"] += 1
                logger.warning(
                    "Queue %s full, dropped oldest item to keep newest data: %s",
                    name,
                    dropped,
                )
                return
            except (queue.Empty, queue.Full):
                pass

        self._channel_stats[name]["dropped_newest"] += 1
        self._channel_stats[name]["full_errors"] += 1
        logger.error("Queue %s full, dropped newest item: %s", name, item)
        self._check_drop_rate_alert(name)

    def _check_drop_rate_alert(self, name: str) -> None:
        """若 60s 内单通道丢弃数超过阈值，发出 ERROR 级聚合告警（DLQ 替代机制）。

        对比两次告警窗口间的累计丢弃量，超过 _drop_alert_rate_threshold 则
        记录带统计数据的 ERROR 日志，便于监控系统（日志聚合/告警规则）捕获。
        """
        stats = self._channel_stats.get(name, {})
        total_drops = stats.get("dropped_oldest", 0) + stats.get("dropped_newest", 0)
        now = time.time()
        last_at = self._drop_alert_last_at.get(name, 0.0)
        last_total = self._drop_alert_last_total.get(name, 0)
        window = 60.0
        if now - last_at >= window:
            delta = total_drops - last_total
            if delta >= self._drop_alert_rate_threshold:
                logger.error(
                    "DLQ ALERT: channel '%s' dropped %d items in the last %.0fs "
                    "(total_drops=%d, full_errors=%d). "
                    "Consider increasing queue size or reducing ingestion rate.",
                    name,
                    delta,
                    window,
                    total_drops,
                    stats.get("full_errors", 0),
                )
            self._drop_alert_last_at[name] = now
            self._drop_alert_last_total[name] = total_drops

    def _resolve_overflow_policy(self, ch: Dict[str, object]) -> str:
        policy = str(ch.get("overflow_policy", "auto")).strip().lower()
        if policy != "auto":
            return policy
        channel_type = str(ch.get("type", "")).strip().lower()
        if channel_type in {"ohlc", "ohlc_indicators", "economic_calendar", "economic_calendar_updates"}:
            return "block"
        return "drop_oldest"
