from __future__ import annotations

import logging
import queue
import threading
from collections import deque
from datetime import datetime
from typing import Any

from src.config.runtime_identity import RuntimeIdentity
from src.utils.timezone import parse_iso_to_utc as _parse_iso_to_utc

from .event_bus import PipelineEvent, PipelineEventBus

logger = logging.getLogger(__name__)


class PipelineTraceRecorder:
    """将 PipelineEventBus 事件持久化到独立 trace 表。"""

    def __init__(
        self,
        *,
        pipeline_bus: PipelineEventBus,
        db_writer: Any,
        runtime_identity: RuntimeIdentity | None = None,
        batch_size: int = 100,
        flush_interval_seconds: float = 1.0,
        queue_size: int = 2048,
    ) -> None:
        self._pipeline_bus = pipeline_bus
        self._db_writer = db_writer
        self._runtime_identity = runtime_identity
        self._batch_size = max(1, int(batch_size))
        self._flush_interval_seconds = max(0.1, float(flush_interval_seconds))
        self._queue: queue.Queue[tuple] = queue.Queue(maxsize=max(1, int(queue_size)))
        self._pending: deque[tuple] = deque()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._listener_attached = False
        self._dropped_events = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        if not self._listener_attached:
            attached = self._pipeline_bus.add_listener(self._on_event)
            if attached is False:
                raise RuntimeError(
                    "PipelineTraceRecorder failed to attach listener to PipelineEventBus"
                )
            self._listener_attached = True
        self._thread = threading.Thread(
            target=self._run,
            name="pipeline-trace-recorder",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._listener_attached:
            self._pipeline_bus.remove_listener(self._on_event)
            self._listener_attached = False
        if self._thread:
            thread = self._thread
            thread.join(timeout=5.0)
            if thread.is_alive():
                logger.warning(
                    "PipelineTraceRecorder stop timed out; recorder thread still alive"
                )
                return
            self._thread = None
        self._flush(force=True)

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.is_running(),
            "queued": self._queue.qsize(),
            "pending": len(self._pending),
            "dropped_events": self._dropped_events,
        }

    def _on_event(self, event: PipelineEvent) -> None:
        try:
            self._queue.put_nowait(self._to_row(event))
        except queue.Full:
            self._dropped_events += 1
            logger.warning(
                "PipelineTraceRecorder queue full, dropped event trace=%s type=%s",
                event.trace_id,
                event.type,
            )

    def _run(self) -> None:
        # §0cc P2：旧 _run 无顶层 try/except，_flush() 抛一次异常 → 后台线程
        # 崩溃 → pipeline bus 仍发事件但永远不再持久化（trace 失明）。
        # 必须把 drain + flush 的异常隔离在循环内，瞬时 DB/磁盘故障下持续运行。
        while not self._stop.is_set() or not self._queue.empty() or self._pending:
            try:
                self._drain_queue()
                self._flush()
            except Exception:
                logger.exception(
                    "PipelineTraceRecorder: flush failed (pending=%d, queue=%d); "
                    "继续运行，将在下次循环重试",
                    len(self._pending),
                    self._queue.qsize(),
                )
            self._stop.wait(0.1)

    def _drain_queue(self) -> None:
        while len(self._pending) < self._batch_size:
            try:
                self._pending.append(self._queue.get_nowait())
            except queue.Empty:
                return

    def _flush(self, *, force: bool = False) -> None:
        if not self._pending:
            return
        if not force and len(self._pending) < self._batch_size:
            oldest = self._pending[0]
            recorded_at = oldest[5]
            if isinstance(recorded_at, datetime):
                age = (datetime.now(recorded_at.tzinfo) - recorded_at).total_seconds()
                if age < self._flush_interval_seconds:
                    return
        batch = list(self._pending)
        self._db_writer.write_pipeline_trace_events(batch, page_size=self._batch_size)
        self._pending.clear()

    def _to_row(self, event: PipelineEvent) -> tuple:
        payload = dict(event.payload or {})
        runtime_identity = self._runtime_identity
        account_key = (
            payload.get("target_account_key")
            or payload.get("account_key")
            or (runtime_identity.account_key if runtime_identity is not None else None)
        )
        return (
            str(event.trace_id),
            str(event.symbol),
            str(event.timeframe),
            str(event.scope),
            str(event.type),
            # §0w R4：写库 TIMESTAMPTZ 列必须保持绝对时刻；event.ts 可能是
            # aware ISO（如生产事件携带 +08:00 偏移），旧 fromisoformat() 直接
            # 落库会让 PG 把 naive 当 server local TZ，aware 当原始 TZ，导致
            # 跨实例时间线错位。统一走 parse_iso_to_utc 强制 UTC。
            _parse_iso_to_utc(str(event.ts)),
            payload,
            runtime_identity.instance_id if runtime_identity is not None else None,
            runtime_identity.instance_role if runtime_identity is not None else None,
            account_key,
            payload.get("signal_id"),
            payload.get("intent_id"),
            payload.get("command_id"),
            payload.get("action_id"),
        )
