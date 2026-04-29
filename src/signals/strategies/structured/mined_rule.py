"""MinedRuleStrategy — 数据驱动的策略框架。

把 `MiningRunner` 输出的 `mined_rules` 直接转成可运行的策略实例，避免
每条 rule 手工编码独立 `.py` 文件。

设计要点：
- spec 是不可变值对象（frozen dataclass），可序列化为 YAML/JSON 持久化
- conditions 评估等价于 mining 时的 entry 条件检查
- _when 直接 valid（mining 已把入场触发收进 conditions）
- _exit_spec 用 spec.barrier 配置（barrier mode + 固定 SL/TP/Time）
- required_indicators 自动从 conditions 推导 + atr14（barrier 计算用）
- research_provenance_refs 从 spec.mining_run_id 注入，合规追溯

使用：
    spec = MinedRuleSpec(
        name="structured_mined_h1_buy_3",
        direction="buy",
        timeframe="H1",
        conditions=(
            MinedRuleCondition(indicator="adx14", field="minus_di",
                               op="<=", threshold=19.38),
            ...
        ),
        barrier=MinedRuleBarrier(sl_atr=2.0, tp_atr=3.0, time_bars=80),
        mining_run_id="mine_xxx_2026-04-28",
    )
    strat = MinedRuleStrategy(spec)
    catalog.register(strat)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext
from .base import (
    EntrySpec,
    EntryType,
    ExitMode,
    ExitSpec,
    HtfPolicy,
    StructuredStrategyBase,
)

_VALID_OPS = frozenset((">", ">=", "<", "<=", "==", "!="))


@dataclass(frozen=True)
class MinedRuleCondition:
    """单条入场条件。

    op ∈ {"<", "<=", ">", ">=", "==", "!="}。其它值在构造时 raise ValueError。
    """

    indicator: str
    field: str
    op: str
    threshold: float

    def __post_init__(self) -> None:
        if self.op not in _VALID_OPS:
            raise ValueError(
                f"MinedRuleCondition.op must be one of {sorted(_VALID_OPS)}, "
                f"got {self.op!r}"
            )


@dataclass(frozen=True)
class MinedRuleBarrier:
    """barrier exit 配置（与 mining barrier_predictive_power 对齐）。"""

    sl_atr: float
    tp_atr: float
    time_bars: int


@dataclass(frozen=True)
class MinedRuleSpec:
    """单条 mined rule 的配置规格。

    可以从 mining JSON 自动提取，或手工编写 YAML。
    """

    name: str
    direction: Literal["buy", "sell"]
    timeframe: str
    conditions: Tuple[MinedRuleCondition, ...]
    barrier: MinedRuleBarrier

    # Provenance 追溯字段（来自 mining run）
    mining_run_id: Optional[str] = None
    train_wr: float = 0.0
    test_wr: float = 0.0
    train_n: int = 0
    test_n: int = 0
    barrier_wr: float = 0.0
    train_mean_return: float = 0.0


def _eval_op(value: float, op: str, threshold: float) -> bool:
    """评估单个比较操作。"""
    if op == ">":
        return value > threshold
    if op == ">=":
        return value >= threshold
    if op == "<":
        return value < threshold
    if op == "<=":
        return value <= threshold
    if op == "==":
        return value == threshold
    if op == "!=":
        return value != threshold
    raise ValueError(f"unsupported op: {op!r}")


class MinedRuleStrategy(StructuredStrategyBase):
    """从 MinedRuleSpec 配置的策略。"""

    category: str = "mined_rule"
    htf_policy: HtfPolicy = HtfPolicy.NONE
    preferred_scopes: Tuple[str, ...] = ("confirmed",)

    # 默认 regime_affinity 全 1.0：mining 已经在每个 regime 下评估过 rule，
    # 不需要再做软门控。如果某条 rule 显式只在某 regime 有效，可在 spec
    # 扩展（未来需求）通过 regime filter 实现。
    regime_affinity = {
        RegimeType.TRENDING: 1.0,
        RegimeType.BREAKOUT: 1.0,
        RegimeType.RANGING: 1.0,
        RegimeType.UNCERTAIN: 1.0,
    }

    def __init__(self, spec: MinedRuleSpec) -> None:
        super().__init__(name=spec.name)
        self._spec = spec
        # required_indicators 从 conditions 自动推导（含 atr14 用于 barrier）
        cond_inds = {c.indicator for c in spec.conditions}
        cond_inds.add("atr14")
        self.required_indicators = tuple(sorted(cond_inds))
        # provenance 注入
        if spec.mining_run_id:
            self.research_provenance_refs = (spec.mining_run_id,)

    # ── 评估 ──────────────────────────────────────────────────────────

    def _why(self, ctx: SignalContext) -> Tuple[bool, Optional[str], float, str]:
        """所有 conditions 满足 → 返回 spec.direction。"""
        for c in self._spec.conditions:
            ind_data = ctx.indicators.get(c.indicator)
            if ind_data is None:
                return (
                    False,
                    None,
                    0.0,
                    f"missing:{c.indicator}",
                )
            v = ind_data.get(c.field)
            if v is None:
                return (
                    False,
                    None,
                    0.0,
                    f"missing:{c.indicator}.{c.field}",
                )
            try:
                fv = float(v)
            except (TypeError, ValueError):
                return (
                    False,
                    None,
                    0.0,
                    f"non_numeric:{c.indicator}.{c.field}",
                )
            if not _eval_op(fv, c.op, c.threshold):
                return (
                    False,
                    None,
                    0.0,
                    f"failed:{c.indicator}.{c.field}{c.op}{c.threshold}",
                )

        return True, self._spec.direction, 1.0, "all_conditions_met"

    def _when(self, ctx: SignalContext, direction: str) -> Tuple[bool, float, str]:
        """mined rules 的 conditions 已涵盖入场触发——_when 不再额外门控。"""
        return True, 1.0, "mined_rule_entry"

    # ── 执行规格 ──────────────────────────────────────────────────────

    def _entry_spec(self, ctx: SignalContext, direction: str) -> EntrySpec:
        """市价入场（barrier 已锁 SL/TP，不需要 limit/stop 价格控制）。"""
        return EntrySpec(entry_type=EntryType.MARKET)

    def _exit_spec(self, ctx: SignalContext, direction: str) -> ExitSpec:
        """barrier mode：复用 spec.barrier 的 SL/TP/Time（mining 已识别的最佳）。"""
        b = self._spec.barrier
        return ExitSpec(
            sl_atr=b.sl_atr,
            tp_atr=b.tp_atr,
            mode=ExitMode.BARRIER,
            time_bars=b.time_bars,
        )
