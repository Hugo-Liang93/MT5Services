"""Lifecycle helpers for UnifiedIndicatorManager background threads."""

from __future__ import annotations

import logging
import threading

from . import event_io
from . import event_loops
from . import intrabar_queue

if False:  # pragma: no cover
    from ..manager import UnifiedIndicatorManager

logger = logging.getLogger(__name__)


def any_thread_alive(manager: "UnifiedIndicatorManager") -> bool:
    """Return True when any managed background thread is alive."""
    for t in (
        manager.state.event_thread,
        manager.state.writer_thread,
        manager.state.intrabar_thread,
        manager.state.reload_thread,
    ):
        if t is not None and t.is_alive():
            return True
    return False


def _join_previous_threads(manager: "UnifiedIndicatorManager") -> bool:
    """Join previous round threads and return True when all are stopped."""
    if not any_thread_alive(manager):
        return True

    logger.warning("IndicatorManager: waiting for previous threads to finish")
    for t in (
        manager.state.writer_thread,
        manager.state.event_thread,
        manager.state.intrabar_thread,
        manager.state.reload_thread,
    ):
        if t is not None and t.is_alive():
            t.join(timeout=5.0)

    if any_thread_alive(manager):
        logger.error("IndicatorManager: previous threads still alive after re-join")
        return False

    manager.state.writer_thread = None
    manager.state.event_thread = None
    manager.state.intrabar_thread = None
    manager.state.reload_thread = None
    return True


def start(manager: "UnifiedIndicatorManager") -> None:
    if not _join_previous_threads(manager):
        return

    manager.state.stop_event.clear()
    manager.event_store.reset_processing_events()

    def _closed_bar_event_sink(symbol, timeframe, bar_time):
        event_io.publish_closed_bar_event(manager, symbol, timeframe, bar_time)

    def _intrabar_listener(symbol, timeframe, bar):
        intrabar_queue.enqueue_intrabar_event(manager, symbol, timeframe, bar)

    manager.state.closed_bar_event_sink = _closed_bar_event_sink
    manager.state.intrabar_listener = _intrabar_listener
    manager.market_service.set_ohlc_event_sink(_closed_bar_event_sink)
    manager.market_service.add_intrabar_listener(_intrabar_listener)

    manager.state.writer_thread = threading.Thread(
        target=event_loops.run_event_writer_loop,
        args=(manager,),
        name="IndicatorEventWriter",
        daemon=True,
    )
    manager.state.writer_thread.start()

    manager.state.event_thread = threading.Thread(
        target=event_loops.run_event_loop,
        args=(manager,),
        name="IndicatorEventLoop",
        daemon=True,
    )
    manager.state.event_thread.start()

    manager.state.intrabar_thread = threading.Thread(
        target=event_loops.run_intrabar_loop,
        args=(manager,),
        name="IndicatorIntrabar",
        daemon=True,
    )
    manager.state.intrabar_thread.start()

    if manager.config.hot_reload:
        manager.state.reload_thread = threading.Thread(
            target=event_loops.run_reload_loop,
            args=(manager,),
            name="IndicatorConfigReloader",
            daemon=True,
        )
        manager.state.reload_thread.start()

    logger.info("UnifiedIndicatorManager started")


def stop(manager: "UnifiedIndicatorManager") -> None:
    manager.state.stop_event.set()
    threads_with_timeout: list[tuple[str, str, threading.Thread | None, float]] = [
        ("writer", "writer_thread", manager.state.writer_thread, 3.0),
        ("event", "event_thread", manager.state.event_thread, 5.0),
        ("intrabar", "intrabar_thread", manager.state.intrabar_thread, 2.0),
        ("reload", "reload_thread", manager.state.reload_thread, 2.0),
    ]
    any_zombie = False
    for name, thread_name, thread, timeout in threads_with_timeout:
        if thread is None:
            continue
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning(
                "IndicatorManager %s thread did not stop within %.1fs, "
                "will be cleaned up on next start()",
                name,
                timeout,
            )
            any_zombie = True
            continue
        setattr(manager.state, thread_name, None)

    manager.market_service.set_ohlc_event_sink(None)
    intrabar_listener = manager.state.intrabar_listener
    if intrabar_listener is not None:
        manager.market_service.remove_intrabar_listener(intrabar_listener)
    manager.state.intrabar_listener = None
    manager.state.closed_bar_event_sink = None
    logger.info("UnifiedIndicatorManager stopped")


def is_running(manager: "UnifiedIndicatorManager") -> bool:
    return bool(manager.state.event_thread and manager.state.event_thread.is_alive())
