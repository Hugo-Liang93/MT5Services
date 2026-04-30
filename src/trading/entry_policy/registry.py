"""EntryPolicyRegistry — strategy×tf → policy 解析中枢。

三级 fallback：
  1. config.strategy_tf_mapping[(strategy, tf)]   per-(strategy, tf) override
  2. config.strategy_mapping[strategy]            per-strategy
  3. config.default_policy                        全局兜底

参数解析两级 merge：
  - policy_tf_params[(policy_name, tf)] over policy_params[policy_name]

Registry 是无状态对象，可被多线程并发调用。装配阶段构造一次，全程注入。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from src.config.models.entry_policy import EntryPolicyConfig
from src.trading.entry_policy.port import EntryPolicy, PolicyParams

logger = logging.getLogger(__name__)


class EntryPolicyNotFoundError(KeyError):
    """请求的 policy 名未在 registry 中注册。"""


class EntryPolicyRegistry:
    """注册中心 + 解析器。"""

    def __init__(
        self,
        policies: Mapping[str, EntryPolicy],
        config: EntryPolicyConfig,
    ) -> None:
        self._policies: dict[str, EntryPolicy] = dict(policies)
        self._config: EntryPolicyConfig = config
        self._validate_invariants()

    # ── 公开端口 ────────────────────────────────────────────────────────

    def resolve(self, strategy: str, timeframe: str) -> EntryPolicy:
        """按三级 fallback 解析 (strategy, tf) → policy 实例。"""
        name = self._resolve_policy_name(strategy, timeframe)
        try:
            return self._policies[name]
        except KeyError as exc:
            raise EntryPolicyNotFoundError(
                f"policy {name!r} requested by mapping but not registered"
            ) from exc

    def resolve_params(self, policy_name: str, timeframe: str) -> PolicyParams:
        """按两级 merge 解析 (policy_name, tf) → 参数字典。"""
        base = dict(self._config.policy_params.get(policy_name, {}))
        per_tf = self._config.policy_tf_params.get((policy_name, timeframe), {})
        base.update(per_tf)
        return base

    def list_policies(self) -> list[str]:
        return sorted(self._policies.keys())

    def list_mapping(self) -> dict[str, str]:
        return dict(self._config.strategy_mapping)

    def list_mapping_per_tf(self) -> dict[tuple[str, str], str]:
        return dict(self._config.strategy_tf_mapping)

    @property
    def fill_semantics_tie_break(self) -> str:
        """ADR-013 P4: 回测同 bar 多 member 触发的 tie-break 策略。

        ADR-006 公开端口：BacktestEngine.check_pending_entries 用此值决定
        OCO group 内的成交优先级（limit_first / stop_first / alpha）。
        """
        return self._config.fill_semantics_tie_break

    def describe(self) -> dict[str, Any]:
        """API/审计端口。返回 registry 全貌。"""
        return {
            "default_policy": self._config.default_policy,
            "registered_policies": [
                self._policies[name].describe() for name in self.list_policies()
            ],
            "strategy_mapping": self.list_mapping(),
            "strategy_tf_mapping": [
                {"strategy": s, "timeframe": tf, "policy": p}
                for (s, tf), p in self.list_mapping_per_tf().items()
            ],
            "fill_semantics_tie_break": self._config.fill_semantics_tie_break,
        }

    # ── 工厂 ────────────────────────────────────────────────────────────

    @classmethod
    def from_components(
        cls,
        policies: Mapping[str, EntryPolicy],
        config: EntryPolicyConfig,
    ) -> "EntryPolicyRegistry":
        return cls(policies=policies, config=config)

    # ── 内部 ────────────────────────────────────────────────────────────

    def _resolve_policy_name(self, strategy: str, timeframe: str) -> str:
        per_tf = self._config.strategy_tf_mapping.get((strategy, timeframe))
        if per_tf is not None:
            return per_tf
        per_strategy = self._config.strategy_mapping.get(strategy)
        if per_strategy is not None:
            return per_strategy
        return self._config.default_policy

    def _validate_invariants(self) -> None:
        """装配期 fail-fast：mapping/参数引用的 policy 都必须已注册。"""
        registered = set(self._policies.keys())

        if self._config.default_policy not in registered:
            raise EntryPolicyNotFoundError(
                f"default_policy={self._config.default_policy!r} not in "
                f"registered policies {sorted(registered)}"
            )

        for strategy, name in self._config.strategy_mapping.items():
            if name not in registered:
                raise EntryPolicyNotFoundError(
                    f"strategy_mapping[{strategy!r}]={name!r} not in "
                    f"registered policies {sorted(registered)}"
                )

        for key, name in self._config.strategy_tf_mapping.items():
            if name not in registered:
                raise EntryPolicyNotFoundError(
                    f"strategy_tf_mapping[{key!r}]={name!r} not in "
                    f"registered policies {sorted(registered)}"
                )

        for name in self._config.policy_params.keys():
            if name not in registered:
                logger.warning(
                    "policy_params section %r references unregistered policy "
                    "(ignored at runtime)",
                    name,
                )

        logger.info(
            "EntryPolicyRegistry assembled: %d policies registered, "
            "%d strategy mappings, %d per-tf overrides",
            len(self._policies),
            len(self._config.strategy_mapping),
            len(self._config.strategy_tf_mapping),
        )


__all__ = [
    "EntryPolicyNotFoundError",
    "EntryPolicyRegistry",
]
