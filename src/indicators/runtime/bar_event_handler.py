from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from ..query_services.runtime import (
    _get_pipeline_bus as _runtime_get_pipeline_bus,
    compute_confirmed_results_for_bars,
    compute_intrabar_results_for_bars,
    get_intrabar_eligible_names,
    get_max_lookback,
    group_indicator_values,
    load_confirmed_bars,
    load_intrabar_bars,
    mark_event_completed,
    mark_event_failed,
    mark_event_skipped,
    publish_intrabar_snapshot,
    select_indicator_names_for_history,
    write_back_results,
)
from src.utils.event_store import ClaimedEvent

logger = logging.getLogger(__name__)



def _extract_ohlc(bar: object) -> Optional[Dict[str, float]]:
    """Safely extract OHLC data from a bar object for pipeline tracing."""
    try:
        return {
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(getattr(bar, "volume", 0)),
        }
    except (AttributeError, TypeError):
        return None

def process_closed_bar_events_batch(
    manager,
    events: List[ClaimedEvent],
    durable_event: bool,
) -> None:
    grouped: Dict[Tuple[str, str], List[ClaimedEvent]] = {}
    for event in events:
        grouped.setdefault((event.symbol, event.timeframe), []).append(event)

    for (symbol, timeframe), grouped_events in grouped.items():
        ordered = sorted(grouped_events, key=lambda item: item.bar_time)
        try:
            process_symbol_timeframe_batch(
                manager,
                symbol,
                timeframe,
                ordered,
                durable_event=durable_event,
            )
        except Exception:
            logger.exception(
                "Failed to process closed-bar batch for %s/%s (%s events)",
                symbol,
                timeframe,
                len(ordered),
            )
            if durable_event:
                for event in ordered:
                    mark_event_failed(manager, event.event_id, "batch_processing_failed")


def process_symbol_timeframe_batch(
    manager,
    symbol: str,
    timeframe: str,
    events: List[ClaimedEvent],
    durable_event: bool,
) -> None:
    if not events:
        return

    bar_times = [event.bar_time for event in events]

    latest_bar_time = bar_times[-1]
    lookback = get_max_lookback(manager)
    bars = manager.market_service.get_ohlc_window(
        symbol,
        timeframe,
        end_time=latest_bar_time,
        limit=lookback + len(bar_times),
    )
    bar_index = {bar.time: idx for idx, bar in enumerate(bars)}
    computed = 0
    skipped_bar_missing = 0
    skipped_insufficient_history = 0
    failed = 0

    pipeline_bus = _runtime_get_pipeline_bus(manager)

    for event in events:
        event_id = event.event_id
        bar_time = event.bar_time
        trace_id = uuid4().hex
        try:
            end_idx = bar_index.get(bar_time)

            # Broadcast: bar closed event received (with OHLC if available)
            if pipeline_bus is not None:
                ohlc = _extract_ohlc(bars[end_idx]) if end_idx is not None else None
                pipeline_bus.emit_bar_closed(
                    trace_id, symbol, timeframe, "confirmed", bar_time,
                    ohlc=ohlc,
                )
            if end_idx is None:
                prefix = load_confirmed_bars(
                    manager,
                    symbol,
                    timeframe,
                    bar_time=bar_time,
                )
                if not prefix or prefix[-1].time != bar_time:
                    logger.debug(
                        "Skipping closed-bar event for %s/%s at %s because the target OHLC bar is unavailable",
                        symbol,
                        timeframe,
                        bar_time,
                    )
                    skipped_bar_missing += 1
                    if durable_event:
                        mark_event_skipped(manager, event_id, "bar_missing")
                    continue
            else:
                prefix = bars[: end_idx + 1]
                prefix = prefix[-lookback:]
            selected_names = select_indicator_names_for_history(manager, len(prefix))
            if not selected_names:
                logger.debug(
                    "Skipping indicator computation for %s/%s at %s due to insufficient history (%s bars)",
                    symbol,
                    timeframe,
                    bar_time,
                    len(prefix),
                )
                skipped_insufficient_history += 1
                if durable_event:
                    mark_event_skipped(manager, event_id, "insufficient_history")
                continue

            results, compute_time_ms = compute_confirmed_results_for_bars(
                manager,
                symbol,
                timeframe,
                prefix,
                bar_time=bar_time,
            )
            if not results:
                skipped_insufficient_history += 1
                if durable_event:
                    mark_event_skipped(manager, event_id, "insufficient_history")
                continue

            # Broadcast: indicator computation completed
            if pipeline_bus is not None:
                pipeline_bus.emit_indicator_computed(
                    trace_id, symbol, timeframe, "confirmed",
                    compute_time_ms, len(results),
                    indicator_names=sorted(results.keys()),
                )

            # Attach trace_id so downstream snapshot_publisher can forward it.
            # Use try/finally to guarantee cleanup — if write_back_results
            # raises, a stale trace_id must not leak into later events.
            manager.set_current_trace_id(trace_id)
            try:
                write_back_results(
                    manager,
                    symbol,
                    timeframe,
                    prefix,
                    results,
                    compute_time_ms,
                    bar_time=bar_time,
                )
            finally:
                manager.set_current_trace_id(None)

            computed += 1
            if durable_event:
                mark_event_completed(manager, event_id)
        except Exception as exc:
            failed += 1
            logger.exception(
                "Failed to process closed-bar event for %s/%s at %s in batch",
                symbol,
                timeframe,
                bar_time,
            )
            if durable_event:
                mark_event_failed(manager, event_id, str(exc))

    logger.info(
        "Processed closed-bar batch for %s/%s: events=%s computed=%s skipped_history=%s skipped_bar_missing=%s failed=%s durable=%s",
        symbol,
        timeframe,
        len(bar_times),
        computed,
        skipped_insufficient_history,
        skipped_bar_missing,
        failed,
        durable_event,
    )


def process_closed_bar_event(
    manager,
    symbol: str,
    timeframe: str,
    bar_time: datetime,
    durable_event: bool,
) -> None:
    pipeline_bus = _runtime_get_pipeline_bus(manager)
    trace_id = uuid4().hex
    try:
        bars = load_confirmed_bars(manager, symbol, timeframe, bar_time=bar_time)

        if pipeline_bus is not None:
            ohlc = _extract_ohlc(bars[-1]) if bars else None
            pipeline_bus.emit_bar_closed(
                trace_id, symbol, timeframe, "confirmed", bar_time, ohlc=ohlc,
            )

        if not bars:
            return
        results, compute_time_ms = compute_confirmed_results_for_bars(
            manager,
            symbol,
            timeframe,
            bars,
            bar_time=bar_time,
        )
        if not results:
            return

        if pipeline_bus is not None:
            pipeline_bus.emit_indicator_computed(
                trace_id, symbol, timeframe, "confirmed",
                compute_time_ms, len(results),
                indicator_names=sorted(results.keys()),
            )

        manager.set_current_trace_id(trace_id)
        try:
            write_back_results(
                manager,
                symbol,
                timeframe,
                bars,
                results,
                compute_time_ms,
                bar_time=bar_time,
            )
        finally:
            manager.set_current_trace_id(None)
    except Exception as exc:
        logger.exception(
            "Failed to process closed-bar event for %s/%s at %s",
            symbol,
            timeframe,
            bar_time,
        )
        if durable_event:
            logger.exception(
                "Unexpected durable single-event path for %s/%s at %s",
                symbol,
                timeframe,
                bar_time,
            )


def process_intrabar_event(
    manager,
    symbol: str,
    timeframe: str,
    bar,
):
    pipeline_bus = _runtime_get_pipeline_bus(manager)
    trace_id = uuid4().hex

    if pipeline_bus is not None:
        pipeline_bus.emit_bar_closed(
            trace_id, symbol, timeframe, "intrabar", bar.time,
            ohlc=_extract_ohlc(bar),
        )

    eligible_names = get_intrabar_eligible_names(manager)
    eligible = list(eligible_names)
    # Intrabar 仅允许计算策略自动推导出的指标子集。
    # 当 eligible 为空时，显式跳过（而不是回退到全量计算）。
    if not eligible:
        logger.debug(
            "Skipping intrabar indicator compute for %s/%s at %s: empty eligible set",
            symbol,
            timeframe,
            getattr(bar, "time", None),
        )
        return {}
    bars = load_intrabar_bars(manager, symbol, timeframe, bar)
    if not bars:
        return {}
    results, _compute_time_ms = compute_intrabar_results_for_bars(
        manager,
        symbol,
        timeframe,
        bars,
        bar_time=bar.time,
        indicator_names=eligible,
    )
    if not bars or not results:
        return {}

    if pipeline_bus is not None:
        pipeline_bus.emit_indicator_computed(
            trace_id, symbol, timeframe, "intrabar",
            _compute_time_ms, len(results),
            indicator_names=sorted(results.keys()),
        )

    grouped = group_indicator_values(manager, results)
    if not grouped:
        return {}

    manager.set_current_trace_id(trace_id)
    try:
        result = publish_intrabar_snapshot(
            manager,
            symbol,
            timeframe,
            bar.time,
            grouped,
        )
    finally:
        manager.set_current_trace_id(None)
    return result

