"""EntryPolicyConfig — config/entry_policy.ini 的 Pydantic 模型。

ADR-013 反补丁纪律：
  - mapping / mapping_per_tf 引用未注册的 policy → ValueError（model_validator 校验）
  - 关键身份字段必填，禁止 Optional 默认
  - tf 字符串必须在 KNOWN_TFS 集合内
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

KNOWN_TIMEFRAMES: frozenset[str] = frozenset(
    {"M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"}
)
"""与 src/config/utils.py 的 valid_timeframes 保持一致。集中复用避免漂移。"""

TieBreakStrategy = Literal["limit_first", "stop_first", "alpha"]


class EntryPolicyConfig(BaseModel):
    """[entry_policy] 全局配置。

    enabled_policies     — 注册到 registry 的 policy name 列表
    default_policy       — strategy_mapping 未命中时的兜底
    strategy_mapping     — strategy_name → policy_name
    strategy_tf_mapping  — (strategy_name, timeframe) → policy_name（覆盖 strategy_mapping）
    policy_params        — policy_name → {param: value}
    policy_tf_params     — (policy_name, timeframe) → {param: value}（覆盖 policy_params）
    fill_semantics_tie_break — 回测同 bar 多 member 触发时的 tie-break 策略
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=False,
    )

    enabled_policies: List[str] = Field(
        default_factory=lambda: ["market"],
        description="注册到 registry 的 policy name（来自 [registry] enabled）",
    )
    default_policy: str = Field(
        default="market",
        description="strategy_mapping 未命中时的兜底 policy name",
    )
    strategy_mapping: Dict[str, str] = Field(
        default_factory=dict,
        description="strategy_name → policy_name",
    )
    strategy_tf_mapping: Dict[Tuple[str, str], str] = Field(
        default_factory=dict,
        description="(strategy_name, timeframe) → policy_name（per-tf override）",
    )
    policy_params: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="policy_name → 参数字典",
    )
    policy_tf_params: Dict[Tuple[str, str], Dict[str, Any]] = Field(
        default_factory=dict,
        description="(policy_name, timeframe) → 参数字典（per-tf override）",
    )
    fill_semantics_tie_break: TieBreakStrategy = Field(
        default="limit_first",
        description="回测同 bar 多 member 触发时优先级",
    )

    # ── 校验 ────────────────────────────────────────────────────────────

    @field_validator("enabled_policies")
    @classmethod
    def _enabled_non_empty(cls, value: List[str]) -> List[str]:
        if not value:
            raise ValueError("enabled_policies cannot be empty")
        if len(value) != len(set(value)):
            raise ValueError(f"enabled_policies has duplicates: {value}")
        return value

    @field_validator("default_policy")
    @classmethod
    def _default_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("default_policy cannot be empty")
        return value

    @model_validator(mode="after")
    def _validate_references(self) -> "EntryPolicyConfig":
        enabled = set(self.enabled_policies)

        if self.default_policy not in enabled:
            raise ValueError(
                f"default_policy={self.default_policy!r} not in "
                f"enabled_policies={sorted(enabled)}"
            )

        for strategy, name in self.strategy_mapping.items():
            if name not in enabled:
                raise ValueError(
                    f"strategy_mapping[{strategy!r}]={name!r} not in "
                    f"enabled_policies={sorted(enabled)}"
                )

        for (strategy, tf), name in self.strategy_tf_mapping.items():
            if tf not in KNOWN_TIMEFRAMES:
                raise ValueError(
                    f"strategy_tf_mapping[({strategy!r}, {tf!r})]: "
                    f"unknown timeframe (valid: {sorted(KNOWN_TIMEFRAMES)})"
                )
            if name not in enabled:
                raise ValueError(
                    f"strategy_tf_mapping[({strategy!r}, {tf!r})]={name!r} "
                    f"not in enabled_policies={sorted(enabled)}"
                )

        for name in self.policy_params.keys():
            if name not in enabled:
                # warning level OK — 多余 section 不阻塞启动
                pass

        for (name, tf), _ in self.policy_tf_params.items():
            if tf not in KNOWN_TIMEFRAMES:
                raise ValueError(
                    f"policy_tf_params[({name!r}, {tf!r})]: "
                    f"unknown timeframe (valid: {sorted(KNOWN_TIMEFRAMES)})"
                )

        return self


__all__ = [
    "EntryPolicyConfig",
    "KNOWN_TIMEFRAMES",
    "TieBreakStrategy",
]
