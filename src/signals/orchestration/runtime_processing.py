from __future__ import annotations

import logging
import queue
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ..evaluation.indicators_helpers import extract_close_price
from ..evaluation.regime import RegimeTracker, RegimeType, SoftRegimeResult
from ..metadata_keys import MetadataKey as MK

if TYPE_CHECKING:
    from .runtime import SignalRuntime

logger = logging.getLogger(__name__)


def dequeue_event(runtime: "SignalRuntime", timeout: float) -> tuple | None:
    try:
        event = runtime._confirmed_events.get_nowait()
        runtime._confirmed_burst_count += 1
        if runtime._confirmed_burst_count >= runtime._CONFIRMED_BURST_LIMIT:
            runtime._confirmed_burst_count = 0
            try:
                intrabar_event = runtime._intrabar_events.get_nowait()
                try:
                    runtime._confirmed_events.put_nowait(event)
                except queue.Full:
                    logger.warning(
                        "Confirmed event queue full, dropping re-queued event"
                    )
                event = intrabar_event
            except queue.Empty:
                pass
        return event
    except queue.Empty:
        runtime._confirmed_burst_count = 0
        try:
            return runtime._intrabar_events.get(timeout=timeout)
        except queue.Empty:
            return None


def is_stale_intrabar(
    scope: str, symbol: str, timeframe: str, metadata: dict[str, Any]
) -> bool:
    if scope != "intrabar":
        return False
    enqueued_raw = metadata.get(MK.ENQUEUED_AT)
    if enqueued_raw is None:
        return False
    try:
        queue_age = time.monotonic() - float(enqueued_raw)
        if queue_age > 300.0:
            logger.debug(
                "Dropping stale intrabar event for %s/%s (queue_age=%.1fs)",
                symbol,
                timeframe,
                queue_age,
            )
            return True
    except (TypeError, ValueError):
        logger.debug(
            "Failed to parse _enqueued_at for %s/%s intrabar event",
            symbol,
            timeframe,
        )
    return False


def apply_filter_chain(
    runtime: "SignalRuntime",
    symbol: str,
    scope: str,
    timeframe: str,
    event_time: datetime,
    indicators: dict[str, dict[str, float]],
    active_sessions: list[str],
    metadata: dict[str, Any],
) -> bool:
    if runtime.filter_chain is None:
        return True
    spread_points = float(metadata.get(MK.SPREAD_POINTS, 0.0))
    trace_id = str(metadata.get(MK.SIGNAL_TRACE_ID) or "").strip()
    allowed, reason = runtime.filter_chain.should_evaluate(
        symbol,
        spread_points=spread_points,
        utc_now=event_time,
        active_sessions=active_sessions,
        indicators=indicators,
    )
    pipeline_bus = runtime.get_pipeline_event_bus()
    category = reason.split(":")[0] if reason else "_pass"
    if pipeline_bus is not None and trace_id:
        pipeline_bus.emit_signal_filter_decided(
            trace_id=trace_id,
            symbol=symbol,
            timeframe=timeframe,
            scope=scope,
            allowed=allowed,
            reason=reason or "",
            category=category,
            spread_points=spread_points,
            active_sessions=active_sessions,
        )
    if allowed:
        scope_stats = runtime._filter_by_scope.setdefault(
            scope, {"passed": 0, "blocked": 0, "blocks": {}}
        )
        scope_stats["passed"] += 1
        runtime._filter_window.append((time.monotonic(), scope, "_pass"))
        return True

    log_fn = logger.info if scope == "confirmed" else logger.debug
    log_fn(
        "Signal evaluation skipped for %s/%s [%s]: %s",
        symbol,
        timeframe,
        scope,
        reason,
    )
    category = reason.split(":")[0] if reason else "unknown"
    scope_stats = runtime._filter_by_scope.setdefault(
        scope, {"passed": 0, "blocked": 0, "blocks": {}}
    )
    scope_stats["blocked"] += 1
    scope_stats["blocks"][reason] = scope_stats["blocks"].get(reason, 0) + 1
    runtime._filter_window.append((time.monotonic(), scope, category))
    return False


def detect_regime(
    runtime: "SignalRuntime",
    indicators: dict[str, dict[str, float]],
    metadata: dict[str, Any],
    active_sessions: list[str],
) -> tuple[RegimeType, dict[str, Any]]:
    soft_regime: SoftRegimeResult | None = None
    if runtime.soft_regime_enabled:
        soft_regime = runtime.regime_detector.detect_soft(indicators)
        regime = soft_regime.dominant_regime
    else:
        regime = runtime.regime_detector.detect(indicators)
    regime_metadata = dict(metadata)
    regime_metadata[MK.REGIME_HARD] = regime.value
    if soft_regime is not None:
        regime_metadata[MK.REGIME_SOFT] = soft_regime.to_dict()
        regime_metadata[MK.REGIME_PROBABILITIES] = {
            item.value: soft_regime.probability(item) for item in RegimeType
        }
    regime_metadata[MK.SESSION_BUCKETS] = list(active_sessions)
    if MK.CLOSE_PRICE not in regime_metadata:
        regime_metadata[MK.CLOSE_PRICE] = extract_close_price(indicators)
    return regime, regime_metadata


def process_next_event(runtime: "SignalRuntime", timeout: float = 0.5) -> bool:
    event = dequeue_event(runtime, timeout)
    if event is None:
        return False

    scope, symbol, timeframe, indicators, metadata = event
    snapshot_time = runtime._parse_event_time(
        metadata.get(MK.SNAPSHOT_TIME, datetime.now(timezone.utc))
    )
    bar_time = runtime._parse_event_time(metadata.get(MK.BAR_TIME, snapshot_time))
    event_time = bar_time if scope == "confirmed" else snapshot_time

    if is_stale_intrabar(scope, symbol, timeframe, metadata):
        runtime._dropped_events += 1
        runtime._intrabar_stale_drops += 1
        runtime._processed_events += 1
        return True

    if (
        runtime.filter_chain is not None
        and runtime.filter_chain.session_filter is not None
    ):
        active_sessions = runtime.filter_chain.session_filter.current_sessions(
            event_time
        )
    else:
        active_sessions = []

    if not apply_filter_chain(
        runtime,
        symbol,
        scope,
        timeframe,
        event_time,
        indicators,
        active_sessions,
        metadata,
    ):
        runtime._processed_events += 1
        runtime._run_count += 1
        runtime._last_run_at = datetime.now(timezone.utc)
        return True

    regime, regime_metadata = detect_regime(
        runtime, indicators, metadata, active_sessions
    )

    with runtime._regime_trackers_lock:
        tracker = runtime._regime_trackers.setdefault(
            (symbol, timeframe), RegimeTracker()
        )
    regime_stability = (
        tracker.update(regime)
        if scope == "confirmed"
        else tracker.stability_multiplier()
    )

    snapshot_decisions = runtime._evaluate_strategies(
        symbol,
        timeframe,
        scope,
        indicators,
        regime,
        regime_metadata,
        event_time,
        bar_time,
        active_sessions,
    )

    runtime._process_voting(
        snapshot_decisions,
        symbol,
        timeframe,
        scope,
        regime,
        regime_stability,
        regime_metadata,
        indicators,
        event_time,
        bar_time,
    )

    runtime._processed_events += 1
    runtime._run_count += 1
    runtime._last_run_at = datetime.now(timezone.utc)
    runtime._last_error = None
    return True


def on_snapshot(
    runtime: "SignalRuntime",
    symbol: str,
    timeframe: str,
    bar_time: datetime,
    indicators: dict[str, dict[str, float]],
    scope: str,
    warmup_checker: Any,
    metadata_builder: Any,
) -> tuple[str, str, str, dict[str, dict[str, float]], dict[str, Any]] | None:
    if scope == "confirmed" and not runtime.enable_confirmed_snapshot:
        return None
    if not warmup_checker(runtime, symbol, timeframe, bar_time, indicators, scope):
        return None

    upstream_trace_id = runtime.snapshot_source.get_current_trace_id()
    metadata = metadata_builder(
        scope=scope,
        symbol=symbol,
        timeframe=timeframe,
        bar_time=bar_time,
        snapshot_source=runtime.snapshot_source,
        trace_id=upstream_trace_id or uuid4().hex,
    )
    return (scope, symbol, timeframe, indicators, metadata)


def enqueue_event(
    runtime: "SignalRuntime",
    item: tuple[str, str, str, dict[str, dict[str, float]], dict[str, Any]],
) -> None:
    item[4]["_enqueued_at"] = time.monotonic()
    scope = item[0]
    target_queue = (
        runtime._confirmed_events if scope == "confirmed" else runtime._intrabar_events
    )
    try:
        target_queue.put_nowait(item)
    except queue.Full:
        if scope == "confirmed":
            try:
                runtime._confirmed_backpressure_waits += 1
                target_queue.put(
                    item,
                    timeout=max(
                        runtime.policy.confirmed_queue_backpressure_timeout_seconds,
                        0.0,
                    ),
                )
                return
            except queue.Full:
                runtime._confirmed_backpressure_failures += 1
        elif runtime._intrabar_trade_coordinator is not None:
            try:
                target_queue.put(item, timeout=0.02)
                runtime._intrabar_overflow_wait_success += 1
                return
            except queue.Full:
                try:
                    target_queue.get_nowait()
                    target_queue.put_nowait(item)
                    runtime._intrabar_overflow_replace_success += 1
                    return
                except queue.Empty:
                    runtime._intrabar_overflow_failures += 1
                except queue.Full:
                    runtime._intrabar_overflow_failures += 1

        runtime._dropped_events += 1
        if scope == "confirmed":
            runtime._dropped_confirmed += 1
            symbol, timeframe = item[1], item[2]
            logger.warning(
                "CONFIRMED event DROPPED for %s/%s (backpressure_failures=%d, "
                "total_confirmed_dropped=%d). Bar-close signal permanently lost!",
                symbol,
                timeframe,
                runtime._confirmed_backpressure_failures,
                runtime._dropped_confirmed,
            )
        else:
            runtime._dropped_intrabar += 1
            if runtime._intrabar_trade_coordinator is not None:
                symbol, timeframe = item[1], item[2]
                logger.warning(
                    "INTRABAR event DROPPED for %s/%s while intrabar_trading "
                    "is enabled (total_intrabar_dropped=%d). "
                    "Coordinator stability counter may be disrupted. "
                    "(wait_success=%d, replace_success=%d, overflow_failures=%d)",
                    symbol,
                    timeframe,
                    runtime._dropped_intrabar,
                    runtime._intrabar_overflow_wait_success,
                    runtime._intrabar_overflow_replace_success,
                    runtime._intrabar_overflow_failures,
                )
        now = time.monotonic()
        if now - runtime._last_drop_log_at >= 60.0:
            delta = runtime._dropped_events - runtime._dropped_at_last_log
            runtime._last_drop_log_at = now
            runtime._dropped_at_last_log = runtime._dropped_events
            symbol, timeframe = item[1], item[2]
            logger.error(
                "Signal runtime %s queue is full - indicator snapshot dropped "
                "(dropped_since_last_log=%d, total_dropped=%d, "
                "scope=%s, symbol=%s, timeframe=%s, maxsize=%d). "
                "Consider increasing queue capacity or reducing event frequency.",
                scope,
                delta,
                runtime._dropped_events,
                scope,
                symbol,
                timeframe,
                target_queue.maxsize,
            )
