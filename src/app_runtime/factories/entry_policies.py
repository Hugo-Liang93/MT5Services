"""EntryPolicyRegistry 装配工厂。

P1: 仅 MarketEntryPolicy
P2: 加 PullbackEntryPolicy / BreakoutEntryPolicy / OcoEntryPolicy / FibPullbackEntryPolicy

注册方式：
  - 通过 BUILT_IN_POLICIES 表声明 (name, factory_callable) 对
  - 工厂从 EntryPolicyConfig.enabled_policies 过滤需要装配的 policy
  - 未在 BUILT_IN_POLICIES 中的 enabled name → 装配阶段 fail-fast
"""

from __future__ import annotations

import logging
from typing import Callable, Mapping

from src.config.models.entry_policy import EntryPolicyConfig
from src.trading.entry_policy import (
    BreakoutEntryPolicy,
    EntryPolicy,
    EntryPolicyRegistry,
    FibPullbackEntryPolicy,
    MarketEntryPolicy,
    OcoEntryPolicy,
    PullbackEntryPolicy,
)

logger = logging.getLogger(__name__)


_BUILT_IN_POLICY_FACTORIES: Mapping[str, Callable[[], EntryPolicy]] = {
    "market": MarketEntryPolicy,
    "pullback": PullbackEntryPolicy,
    "breakout": BreakoutEntryPolicy,
    "oco_pullback_breakout": OcoEntryPolicy,
    "fib_pullback": FibPullbackEntryPolicy,
}


class UnknownEntryPolicyError(KeyError):
    """enabled_policies 引用了 BUILT_IN_POLICY_FACTORIES 中不存在的 policy。"""


def build_entry_policy_registry(config: EntryPolicyConfig) -> EntryPolicyRegistry:
    """根据配置装配 EntryPolicyRegistry。

    fail-fast 规则：
      - enabled_policies 中的每个 name 必须在 _BUILT_IN_POLICY_FACTORIES 内
      - 缺失 → UnknownEntryPolicyError（启动直接崩，不做兜底）
    """
    policies: dict[str, EntryPolicy] = {}
    for name in config.enabled_policies:
        factory = _BUILT_IN_POLICY_FACTORIES.get(name)
        if factory is None:
            raise UnknownEntryPolicyError(
                f"enabled policy {name!r} has no built-in factory; "
                f"available: {sorted(_BUILT_IN_POLICY_FACTORIES.keys())}"
            )
        policies[name] = factory()

    registry = EntryPolicyRegistry.from_components(policies=policies, config=config)
    logger.info(
        "build_entry_policy_registry: assembled %d policies (%s)",
        len(policies),
        sorted(policies.keys()),
    )
    return registry


__all__ = [
    "UnknownEntryPolicyError",
    "build_entry_policy_registry",
]
