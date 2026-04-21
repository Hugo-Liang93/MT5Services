"""IndicatorManager 后台事件循环（从 manager.py 提取的纯函数模块）。

所有函数接收 manager 引用作为显式参数（ADR-002 模式）。
"""

from __future__ import annotations

import logging
import os
import queue
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from src.utils.common import ohlc_key, timeframe_seconds

from ..query_services.runtime import (
    has_reconcile_ready_targets,
    process_intrabar_event,
    reconcile_all,
)
from . import event_io
from .bar_event_handler import process_closed_bar_events_batch
from .intrabar_metrics import (
    record_intrabar_processing_latency_ms,
    record_intrabar_queue_age_ms,
)
from .registry_runtime import _reinitialize

if TYPE_CHECKING:
    from ..manager import UnifiedIndicatorManager

logger = logging.getLogger(__name__)


def _on_thread_crash(thread_name: str, exc: BaseException) -> None:
    """关键运行时线程未捕获异常的 fail-fast handler。

    生产默认：记 CRITICAL 日志 + `os._exit(1)` 立即退出进程，让 supervisor
    重启。历史教训（2026-04-20 事故）：indicator writer thread 在
    `connection pool exhausted` 异常后静默死亡，主进程继续跑 8.5h 僵尸状态，
    API 观测不到问题。fail-fast 是 ADR-004 生命周期契约的落实。

    测试通过 monkeypatch 此函数替换为收集异常的 stub，避免 pytest 被 os._exit 杀掉。
    """
    logger.critical(
        "Indicator runtime thread %s crashed; exiting process so supervisor restarts it",
        thread_name,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    os._exit(1)


def run_event_loop(manager: UnifiedIndicatorManager) -> None:
    """主事件循环：消费 event_store、触发对账、清理旧事件。"""
    try:
        _run_event_loop_body(manager)
    except BaseException as exc:  # noqa: BLE001 — fail-fast 主循环死亡
        _on_thread_crash("IndicatorEventLoop", exc)


def _run_event_loop_body(manager: UnifiedIndicatorManager) -> None:
    reconcile_interval = max(float(manager.config.pipeline.poll_interval), 0.5)
    next_reconcile_at = time.monotonic()
    _cleanup_interval = 86400.0
    _next_cleanup_at = time.monotonic() + _cleanup_interval
    _warmup_done = False
    _warmup_deadline = time.monotonic() + 30.0

    while not manager.state.stop_event.is_set():
        now = time.monotonic()

        events = manager.event_store.claim_next_events(limit=32)
        if events:
            process_closed_bar_events_batch(manager, events, durable_event=True)

        now = time.monotonic()

        if now >= _next_cleanup_at:
            try:
                manager.cleanup_old_events(days_to_keep=7)
            except Exception:
                logger.debug("event_store cleanup failed", exc_info=True)
            _next_cleanup_at = time.monotonic() + _cleanup_interval

        if now >= next_reconcile_at:
            if has_reconcile_ready_targets(manager):
                reconcile_all(manager)
                manager.state.last_reconcile_at = datetime.now(timezone.utc)
                if not _warmup_done:
                    _warmup_done = True
                    logger.info(
                        "Indicator warmup complete: initial reconcile filled %d results",
                        len(manager.state.results),
                    )
            if not _warmup_done and now < _warmup_deadline:
                next_reconcile_at = time.monotonic() + 2.0
            else:
                if not _warmup_done:
                    _warmup_done = True
                    logger.info(
                        "Indicator warmup: deadline reached, proceeding without initial fill"
                    )
                next_reconcile_at = time.monotonic() + reconcile_interval
            continue

        sleep_for = min(0.1, max(0.0, next_reconcile_at - time.monotonic()))
        manager.state.stop_event.wait(sleep_for)


def run_intrabar_loop(manager: UnifiedIndicatorManager) -> None:
    """专用线程：消费 _intrabar_queue 并计算实时指标。"""
    try:
        _run_intrabar_loop_body(manager)
    except BaseException as exc:  # noqa: BLE001 — fail-fast 主循环死亡
        _on_thread_crash("IndicatorIntrabar", exc)


def _run_intrabar_loop_body(manager: UnifiedIndicatorManager) -> None:
    while not manager.state.stop_event.is_set():
        try:
            item = manager.state.intrabar_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        symbol = item.symbol
        timeframe = item.timeframe
        bar = item.bar
        enqueued_at = item.enqueued_at_monotonic
        record_intrabar_queue_age_ms(
            manager,
            (time.monotonic() - enqueued_at) * 1000,
        )

        if not manager.state.snapshot_listeners:
            manager.state.intrabar_no_listener_skips += 1
            now = time.monotonic()
            if (
                now - manager.state.intrabar_no_listener_last_log_at >= 60.0
                and manager.state.intrabar_no_listener_skips > 0
            ):
                logger.warning(
                    "Intrabar event skipped because no snapshot listeners are registered "
                    "(skipped_since_last_log=%d, queue_size=%d)",
                    manager.state.intrabar_no_listener_skips,
                    manager.state.intrabar_queue.qsize(),
                )
                manager.state.intrabar_no_listener_last_log_at = now
                manager.state.intrabar_no_listener_skips = 0
            continue
        key = ohlc_key(symbol, timeframe)
        now = time.monotonic()
        min_gap = max(1.0, timeframe_seconds(timeframe) * 0.02)
        if now - manager.state.last_intrabar_compute.get(key, 0.0) < min_gap:
            continue
        manager.state.last_intrabar_compute[key] = now
        start_time = time.perf_counter()
        try:
            process_intrabar_event(manager, symbol, timeframe, bar)
        except Exception:
            logger.exception(
                "Failed to process intrabar preview for %s/%s at %s",
                symbol,
                timeframe,
                getattr(bar, "time", None),
            )
        finally:
            record_intrabar_processing_latency_ms(
                manager,
                (time.perf_counter() - start_time) * 1000,
            )


def run_event_writer_loop(manager: UnifiedIndicatorManager) -> None:
    """专用线程：将 bar close 事件批量写入 SQLite。"""
    try:
        _run_event_writer_loop_body(manager)
    except BaseException as exc:  # noqa: BLE001 — fail-fast 主循环死亡
        _on_thread_crash("IndicatorEventWriter", exc)


def _run_event_writer_loop_body(manager: UnifiedIndicatorManager) -> None:
    while not manager.state.stop_event.is_set():
        try:
            item = manager.state.event_write_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        event_io.flush_event_batch(manager, item, "loop")
    # Drain remaining items on shutdown.
    while True:
        try:
            item = manager.state.event_write_queue.get_nowait()
        except queue.Empty:
            break
        event_io.flush_event_batch(manager, item, "shutdown")


def run_reload_loop(manager: UnifiedIndicatorManager) -> None:
    """配置热重载循环。"""
    try:
        _run_reload_loop_body(manager)
    except BaseException as exc:  # noqa: BLE001 — fail-fast 主循环死亡
        _on_thread_crash("IndicatorConfigReloader", exc)


def _run_reload_loop_body(manager: UnifiedIndicatorManager) -> None:
    reload_interval = max(float(manager.config.reload_interval), 1.0)
    while not manager.state.stop_event.wait(reload_interval):
        try:
            if manager.config_manager.reload():
                manager.config = manager.config_manager.get_config()
                _reinitialize(manager)
        except Exception:
            logger.exception("Indicator config reload failed")
