"""Delta metrics computation extracted from UnifiedIndicatorManager.

Computes N-bar change rates (first-order derivatives) for configured indicators.
Pure functions that can be used by both live and backtest modes.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def get_delta_config(
    indicator_configs: Any,
) -> Dict[str, tuple[int, ...]]:
    """Build a mapping of indicator_name -> tuple of delta bar counts.

    Reads ``delta_bars`` from each enabled indicator config entry.
    """
    config_items = indicator_configs or []
    mapping: Dict[str, tuple[int, ...]] = {}
    for config in config_items:
        if not config.enabled or not getattr(config, "delta_bars", None):
            continue
        mapping[config.name] = tuple(
            sorted({int(item) for item in config.delta_bars if int(item) > 0})
        )
    return mapping


def apply_delta_metrics(
    symbol: str,
    timeframe: str,
    indicators: Dict[str, Dict[str, float]],
    delta_config: Dict[str, tuple[int, ...]],
    history_loader: Callable[[str, str, Optional[Any], int], List[Any]],
    *,
    bar_time: Optional[Any] = None,
) -> Dict[str, Dict[str, float]]:
    """Compute N-bar deltas for indicators that have delta_bars configured.

    Args:
        symbol: Trading symbol.
        timeframe: Timeframe string.
        indicators: Current indicator values keyed by name.
        delta_config: Mapping of indicator_name -> tuple of delta offsets.
        history_loader: Callable(symbol, timeframe, bar_time, count) -> list of bars.
        bar_time: Optional bar timestamp for windowed lookback.

    Returns:
        The indicators dict enriched with ``{metric}_d{N}`` delta fields.
    """
    if not delta_config or not indicators:
        return indicators

    max_delta = max((max(values) for values in delta_config.values()), default=0)
    if max_delta <= 0:
        return indicators

    try:
        history = history_loader(symbol, timeframe, bar_time, max_delta + 1)
    except Exception:
        logger.debug(
            "Failed to load historical bars for delta metrics: %s/%s",
            symbol,
            timeframe,
            exc_info=True,
        )
        return indicators

    if not history:
        return indicators

    current_in_history = bool(bar_time is not None and history[-1].time == bar_time)
    offset = 1 if current_in_history else 0

    for indicator_name, payload in indicators.items():
        delta_bars = delta_config.get(indicator_name)
        if not delta_bars or not isinstance(payload, dict):
            continue
        for delta in delta_bars:
            ref_index = -(delta + offset)
            if len(history) < delta + offset:
                continue
            try:
                reference_bar = history[ref_index]
            except IndexError:
                continue
            reference_payload = getattr(reference_bar, "indicators", None) or {}
            reference_indicator = reference_payload.get(indicator_name)
            if not isinstance(reference_indicator, dict):
                continue
            for metric_name, current_value in list(payload.items()):
                if not isinstance(current_value, (int, float)):
                    continue
                previous_value = reference_indicator.get(metric_name)
                if not isinstance(previous_value, (int, float)):
                    continue
                payload[f"{metric_name}_d{delta}"] = round(
                    float(current_value) - float(previous_value),
                    6,
                )
    return indicators


def merge_snapshot_metrics_into_results(
    symbol: str,
    timeframe: str,
    indicators: Dict[str, Dict[str, float]],
    results: Any,
    results_lock: Optional[Any] = None,
) -> None:
    """Merge delta-enriched indicators back into the results store.

    This updates existing IndicatorResult.value dicts in-place so that
    get_indicator() returns the enriched metrics without recomputation.
    """
    prefix = f"{symbol}_{timeframe}_"
    if not isinstance(results, dict) or not results:
        return

    def _merge() -> None:
        for indicator_name, payload in indicators.items():
            result = results.get(f"{prefix}{indicator_name}")
            if result is None or not isinstance(result.value, dict):
                continue
            result.value.update(payload)

    if results_lock is None:
        _merge()
        return
    with results_lock:
        _merge()
