from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Tuple

from src.utils.event_store import ClaimedEvent

logger = logging.getLogger(__name__)

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
                    manager._mark_event_failed(event.event_id, "batch_processing_failed")


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
    lookback = manager._get_max_lookback()
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

    for event in events:
        event_id = event.event_id
        bar_time = event.bar_time
        try:
            end_idx = bar_index.get(bar_time)
            if end_idx is None:
                prefix = manager._load_confirmed_bars(symbol, timeframe, bar_time=bar_time)
                if not prefix or prefix[-1].time != bar_time:
                    logger.debug(
                        "Skipping closed-bar event for %s/%s at %s because the target OHLC bar is unavailable",
                        symbol,
                        timeframe,
                        bar_time,
                    )
                    skipped_bar_missing += 1
                    if durable_event:
                        manager._mark_event_skipped(event_id, "bar_missing")
                    continue
            else:
                prefix = bars[: end_idx + 1]
                prefix = prefix[-lookback:]
            selected_names = manager._select_indicator_names_for_history(len(prefix))
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
                    manager._mark_event_skipped(event_id, "insufficient_history")
                continue

            results, compute_time_ms = manager._compute_confirmed_results_for_bars(
                symbol,
                timeframe,
                prefix,
                bar_time=bar_time,
            )
            if not results:
                skipped_insufficient_history += 1
                if durable_event:
                    manager._mark_event_skipped(event_id, "insufficient_history")
                continue
            manager._write_back_results(
                symbol,
                timeframe,
                prefix,
                results,
                compute_time_ms,
                bar_time=bar_time,
            )
            computed += 1
            if durable_event:
                manager._mark_event_completed(event_id)
        except Exception as exc:
            failed += 1
            logger.exception(
                "Failed to process closed-bar event for %s/%s at %s in batch",
                symbol,
                timeframe,
                bar_time,
            )
            if durable_event:
                manager._mark_event_failed(event_id, str(exc))

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
    try:
        bars = manager._load_confirmed_bars(symbol, timeframe, bar_time=bar_time)
        if not bars:
            return
        results, compute_time_ms = manager._compute_confirmed_results_for_bars(
            symbol,
            timeframe,
            bars,
            bar_time=bar_time,
        )
        if not results:
            return
        manager._write_back_results(
            symbol,
            timeframe,
            bars,
            results,
            compute_time_ms,
            bar_time=bar_time,
        )
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
    eligible_names = manager._get_intrabar_eligible_names()
    eligible = list(eligible_names) if eligible_names else None
    bars = manager._load_intrabar_bars(symbol, timeframe, bar)
    if not bars:
        return {}
    results, _compute_time_ms = manager._compute_intrabar_results_for_bars(
        symbol,
        timeframe,
        bars,
        bar_time=bar.time,
        indicator_names=eligible,
    )
    if not bars or not results:
        return {}
    grouped = manager._group_indicator_values(results)
    if not grouped:
        return {}
    return manager._publish_intrabar_snapshot(symbol, timeframe, bar.time, grouped)
