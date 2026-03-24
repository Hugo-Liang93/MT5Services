"""Bar loading and indicator history requirement calculations.

Pure functions for determining how many historical bars each indicator needs.
These can be reused by backtesting without instantiating the full manager.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def indicator_history_requirement(config: Any) -> int:
    """Compute the minimum number of historical bars required for one indicator.

    Inspects known parameter names (period, slow, fast, etc.) in the indicator
    config and returns the largest value found, with a floor of 2.
    """
    params = getattr(config, "params", {}) or {}
    return max(
        *(
            int(params.get(key, 0) or 0)
            for key in (
                "min_bars",
                "period",
                "slow",
                "signal",
                "fast",
                "atr_period",
                "k_period",
                "d_period",
                "lookback",
            )
        ),
        2,
    )


def resolve_indicator_names(
    indicator_configs: Any,
    indicator_names: Optional[List[str]] = None,
) -> List[str]:
    """Return the list of indicator names to compute.

    If *indicator_names* is provided, returns it as-is.
    Otherwise returns all enabled indicator names from config.
    """
    if indicator_names is not None:
        return indicator_names
    return [config.name for config in indicator_configs if config.enabled]


def indicator_requirements(
    indicator_configs: Any,
    indicator_names: Optional[List[str]] = None,
) -> Dict[str, int]:
    """Compute history requirements for each selected indicator.

    Returns a mapping of indicator_name -> minimum bars needed.
    """
    selected_names = set(resolve_indicator_names(indicator_configs, indicator_names))
    if indicator_configs is None:
        return {name: 2 for name in selected_names}
    requirements: Dict[str, int] = {}
    for config in indicator_configs:
        if not config.enabled or config.name not in selected_names:
            continue
        requirements[config.name] = indicator_history_requirement(config)
    return requirements


def select_indicator_names_for_history(
    indicator_configs: Any,
    available_bars: int,
    indicator_names: Optional[List[str]] = None,
) -> List[str]:
    """Filter indicators to those whose history requirement fits in available bars."""
    reqs = indicator_requirements(indicator_configs, indicator_names)
    return [
        name
        for name in resolve_indicator_names(indicator_configs, indicator_names)
        if reqs.get(name, 2) <= available_bars
    ]


def get_max_lookback(indicator_configs: Any) -> int:
    """Return the maximum lookback across all enabled indicators."""
    return max(indicator_requirements(indicator_configs).values(), default=2)


def get_min_required_history(indicator_configs: Any) -> int:
    """Return the minimum history requirement across all enabled indicators (floor 2)."""
    return max(min(indicator_requirements(indicator_configs).values(), default=2), 2)


def reconcile_min_bars(indicator_configs: Any) -> int:
    """Minimum bar count needed for a reconciliation pass."""
    return min(max(get_max_lookback(indicator_configs), 2), 100)
