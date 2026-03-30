from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple


def run_pipeline(
    manager,
    symbol: str,
    timeframe: str,
    indicator_names: Optional[List[str]] = None,
    bar_time=None,
) -> Tuple[List[Any], Dict[str, Dict[str, Any]], float]:
    bars = manager._load_confirmed_bars(symbol, timeframe, bar_time=bar_time)
    if len(bars) < 2:
        return [], {}, 0.0

    selected_names = manager._select_indicator_names_for_history(len(bars), indicator_names)
    if not selected_names:
        return bars, {}, 0.0
    started_at = time.time()
    results = manager.pipeline.compute(symbol, timeframe, bars, selected_names)
    compute_time_ms = (time.time() - started_at) * 1000
    return bars, results, compute_time_ms


def compute_with_bars(
    manager,
    symbol: str,
    timeframe: str,
    bars: List[Any],
    indicator_names: Optional[List[str]] = None,
) -> Tuple[Dict[str, Dict[str, Any]], float, List[str]]:
    if len(bars) < 2:
        return {}, 0.0, []

    selected_names = manager._select_indicator_names_for_history(len(bars), indicator_names)
    if not selected_names:
        return {}, 0.0, []

    started_at = time.time()
    results = manager.pipeline.compute(symbol, timeframe, bars, selected_names)
    compute_time_ms = (time.time() - started_at) * 1000
    return results, compute_time_ms, selected_names


def compute_results_with_priority_groups(
    manager,
    symbol: str,
    timeframe: str,
    bars: List[Any],
    *,
    bar_time,
    scope: str,
    indicator_names: Optional[List[str]] = None,
) -> Tuple[Dict[str, Dict[str, Any]], float]:
    if len(bars) < 2:
        return {}, 0.0

    selected_names = manager._select_indicator_names_for_history(len(bars), indicator_names)
    if not selected_names:
        return {}, 0.0

    # No on_level_complete callback: partial snapshots caused strategies to
    # log "missing indicators" warnings for indicators in other groups.
    # Full snapshots are published by the caller:
    # - confirmed: _write_back_results()
    # - intrabar: _handle_intrabar_event()
    started_at = time.time()
    compute_staged = getattr(manager.pipeline, "compute_staged", None)
    if callable(compute_staged):
        try:
            results = compute_staged(
                symbol,
                timeframe,
                bars,
                indicators=selected_names,
                scope=scope,
            )
        except TypeError as exc:
            if "scope" not in str(exc):
                raise
            results = compute_staged(
                symbol,
                timeframe,
                bars,
                indicators=selected_names,
            )
    else:
        results = manager.pipeline.compute(symbol, timeframe, bars, selected_names)
    compute_time_ms = (time.time() - started_at) * 1000
    return results, compute_time_ms


def compute_priority_results(
    manager,
    symbol: str,
    timeframe: str,
    bars: List[Any],
    *,
    bar_time,
    scope: str,
    selected_names: Optional[List[str]] = None,
) -> Tuple[Dict[str, Dict[str, Any]], float, set[str]]:
    if len(bars) < 2:
        return {}, 0.0, set()

    selected = selected_names or manager._select_indicator_names_for_history(len(bars))
    if not selected:
        return {}, 0.0, set()

    selected_set = set(selected)
    priority_groups = [
        indicator_group
        for indicator_group in getattr(manager, "_priority_indicator_groups", ())
        if indicator_group and all(indicator_name in selected_set for indicator_name in indicator_group)
    ]
    if not priority_groups:
        return {}, 0.0, set()

    merged_results: Dict[str, Dict[str, Any]] = {}
    covered_names: set[str] = set()
    total_compute_time_ms = 0.0
    for indicator_group in priority_groups:
        group_results, compute_time_ms, _group_selected = manager._compute_with_bars(
            symbol,
            timeframe,
            bars,
            indicator_names=list(indicator_group),
        )
        total_compute_time_ms += compute_time_ms
        if not group_results:
            continue
        grouped = manager._group_indicator_values(group_results)
        if not grouped or any(indicator_name not in grouped for indicator_name in indicator_group):
            continue
        merged_results.update(group_results)
        covered_names.update(indicator_group)

    return merged_results, total_compute_time_ms, covered_names
