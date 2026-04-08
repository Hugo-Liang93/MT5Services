from __future__ import annotations

import logging
import queue
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from src.indicators.accessor import get_registry as _get_indicator_registry

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
    pipeline_bus = getattr(runtime, "_pipeline_event_bus", None)
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
    if runtime._soft_regime_enabled:
        soft_regime = runtime._regime_detector.detect_soft(indicators)
        regime = soft_regime.dominant_regime
    else:
        regime = runtime._regime_detector.detect(indicators)
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

    # 首个 confirmed 事件处理完毕后标记 registry 收敛，
    # 后续 confirmed 指标计算将按需执行（仅计算被引用的指标）。
    if scope == "confirmed":
        _indicator_registry = _get_indicator_registry()
        if not _indicator_registry.converged:
            _indicator_registry.mark_converged()
            required = sorted(_indicator_registry.required_set())
            logger.info(
                "IndicatorRegistry converged after first confirmed event. "
                "Required indicators (%d): %s",
                len(required),
                required,
            )

    return True
