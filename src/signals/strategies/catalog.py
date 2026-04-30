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
    StructuredPriorDayRetest,
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
        StructuredPriorDayRetest(),
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
) -> dict[str, list[str]]:
    """从 mining JSON 加载 mined rule specs 并注册为 MinedRuleStrategy。

    Args:
        catalog: 现有 catalog（会被原地修改）
        json_paths: mining JSON 文件路径列表（mining_runner --json-output 产物）
        promote_only: True (默认) 仅注册通过 PROMOTION_GATES 的 spec；
                      False 用于研究审视（绕过门禁）

    Returns:
        `{spec.name: [spec.timeframe]}` 映射，仅含本次实际新注册的 spec
        （已存在的 name 不进入 map，且不覆盖手工策略）。装配层（CLI / API）
        把此 map 合并进 `BacktestConfig.strategy_timeframes` 白名单，
        让每条 mined spec 仅在自己 mining TF 上运行——避免 H4 spec 被
        H1/M30 pipeline 误评估造成 payload 语义错位（cross-TF 失真）。

    缺失文件静默跳过（不 raise），便于多 source 容错。
    """
    from .structured.mined_rule import MinedRuleStrategy
    from .structured.mined_rule_loader import filter_promotable, load_specs_from_path

    timeframe_map: dict[str, list[str]] = {}
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
            timeframe_map[spec.name] = [spec.timeframe]
        logger.info(
            "register_mined_rule_strategies: %s → registered %d specs (promote_only=%s)",
            path.name,
            len(specs),
            promote_only,
        )
    return timeframe_map
