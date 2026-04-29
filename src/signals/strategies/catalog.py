"""Signals 策略目录入口。"""

from __future__ import annotations

import logging
from collections import OrderedDict
from pathlib import Path
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
    StructuredStrongTrendFollow,
    StructuredSweepReversal,
    StructuredTrendContinuation,
    StructuredTrendlineTouch,
)

logger = logging.getLogger(__name__)


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
        StructuredStrongTrendFollow(),
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


def register_mined_rule_strategies(
    catalog: "OrderedDict[str, SignalStrategy]",
    json_paths: Iterable[Path],
    *,
    promote_only: bool = True,
) -> int:
    """从 mining JSON 加载 mined rule specs 并注册为 MinedRuleStrategy。

    Args:
        catalog: 现有 catalog（会被原地修改）
        json_paths: mining JSON 文件路径列表（mining_runner --json-output 产物）
        promote_only: True (默认) 仅注册通过 PROMOTION_GATES 的 spec；
                      False 用于研究审视（绕过门禁）

    Returns:
        实际注册数量（已存在的 name 跳过，不覆盖手工策略）

    缺失文件静默跳过（不 raise），便于多 source 容错。
    """
    from .structured.mined_rule import MinedRuleStrategy
    from .structured.mined_rule_loader import filter_promotable, load_specs_from_path

    registered = 0
    for path in json_paths:
        path = Path(path)
        if not path.exists():
            logger.warning(
                "register_mined_rule_strategies: skipping missing path %s", path
            )
            continue
        specs = load_specs_from_path(path)
        if promote_only:
            specs = filter_promotable(specs)
        for spec in specs:
            if spec.name in catalog:
                continue  # 已存在 → 不覆盖（防止意外替换手工策略）
            catalog[spec.name] = MinedRuleStrategy(spec)
            registered += 1
        logger.info(
            "register_mined_rule_strategies: %s → registered %d specs (promote_only=%s)",
            path.name,
            len(specs),
            promote_only,
        )
    return registered
