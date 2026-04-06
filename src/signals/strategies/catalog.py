from __future__ import annotations

from collections import OrderedDict
from typing import Any, Iterable, Optional

from .base import SignalStrategy
from .structured import (
    StructuredBreakoutFollow,
    StructuredLowbarEntry,
    StructuredRangeReversion,
    StructuredSessionBreakout,
    StructuredSweepReversal,
    StructuredTrendContinuation,
    StructuredTrendlineTouch,
)


def build_named_strategy_catalog(
    *,
    htf_cache: Optional[Any] = None,
    include_composites: bool = True,
) -> "OrderedDict[str, SignalStrategy]":
    strategies: "OrderedDict[str, SignalStrategy]" = OrderedDict()

    for strategy in (
        StructuredTrendContinuation(),
        StructuredTrendContinuation(name="structured_trend_h4", htf="H4"),
        StructuredSweepReversal(),
        StructuredBreakoutFollow(),
        StructuredRangeReversion(),
        StructuredSessionBreakout(),
        StructuredTrendlineTouch(),
        StructuredLowbarEntry(),
    ):
        strategies[strategy.name] = strategy

    return strategies


def build_default_strategy_set(
    *,
    htf_cache: Optional[Any] = None,
    include_composites: bool = True,
) -> list[SignalStrategy]:
    return list(
        build_named_strategy_catalog(
            htf_cache=htf_cache,
            include_composites=include_composites,
        ).values()
    )


def clone_registered_strategies(
    strategy_names: Iterable[str],
    *,
    htf_cache: Optional[Any] = None,
    include_composites: bool = True,
) -> list[SignalStrategy]:
    catalog = build_named_strategy_catalog(
        htf_cache=htf_cache,
        include_composites=include_composites,
    )
    cloned: list[SignalStrategy] = []
    missing: list[str] = []
    for name in strategy_names:
        strategy = catalog.get(str(name))
        if strategy is None:
            missing.append(str(name))
            continue
        cloned.append(strategy)
    if missing:
        raise ValueError(
            "Unregistered strategies requested from catalog: "
            + ", ".join(sorted(missing))
        )
    return cloned
