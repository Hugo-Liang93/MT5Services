"""Signals 策略目录入口。"""

from __future__ import annotations

from collections import OrderedDict
from typing import Iterable

from .base import SignalStrategy
from .structured import (
    StructuredBreakoutFollow,
    StructuredLowbarEntry,
    StructuredOpenRangeBreakout,
    StructuredPriceAction,
    StructuredPullbackWindow,
    StructuredRangeReversion,
    StructuredRegimeExhaustion,
    StructuredSessionBreakout,
    StructuredSweepReversal,
    StructuredTrendContinuation,
    StructuredTrendlineTouch,
)


def _build_structured_strategies() -> tuple[SignalStrategy, ...]:
    """返回当前激活结构化策略的 fresh 实例，避免跨运行时共享 stateful 对象。"""
    return (
        StructuredTrendContinuation(),
        StructuredTrendContinuation(name="structured_trend_h4", htf="H4"),
        StructuredTrendContinuation(
            name="structured_trend_h4_momentum",
            htf="H4",
            use_momentum_consensus=True,
        ),
        StructuredSweepReversal(),
        StructuredBreakoutFollow(),
        StructuredRangeReversion(),
        StructuredSessionBreakout(),
        StructuredTrendlineTouch(),
        StructuredLowbarEntry(),
        StructuredPullbackWindow(),
        StructuredOpenRangeBreakout(),
        StructuredPriceAction(),
        StructuredRegimeExhaustion(),
    )


def build_named_strategy_catalog() -> "OrderedDict[str, SignalStrategy]":
    # Structured strategies are the only active strategy source in the current runtime.
    strategies: "OrderedDict[str, SignalStrategy]" = OrderedDict()

    for strategy in _build_structured_strategies():
        strategies[strategy.name] = strategy

    return strategies


def build_default_strategy_set() -> list[SignalStrategy]:
    catalog = build_named_strategy_catalog()
    return list(catalog.values())


def clone_registered_strategies(strategy_names: Iterable[str]) -> list[SignalStrategy]:
    catalog = build_named_strategy_catalog()
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
