"""MinedRuleStrategy: 数据驱动的策略框架测试。

按 spec dict 配置的通用策略——把 mining 输出的 mined_rules 直接转成可运行的
StructuredStrategyBase 实例，避免每条 rule 手工编码一个独立 .py 文件。

设计要点：
- spec.conditions 全部满足 → _why 通过，方向由 spec.direction 固定
- _when 始终 valid（mined rules 已经把入场触发收进 conditions）
- _exit_spec 由 spec.barrier 配置（barrier mode + 固定 SL/TP/Time）
- required_indicators 从 conditions 自动推导
- spec 不可变（frozen dataclass）便于审计与 hash
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from src.signals.evaluation.regime import RegimeType
from src.signals.models import SignalContext
from src.signals.strategies.structured.base import (
    EntryType,
    ExitMode,
    HtfPolicy,
    StructuredStrategyBase,
)
from src.signals.strategies.structured.mined_rule import (
    MinedRuleBarrier,
    MinedRuleCondition,
    MinedRuleSpec,
    MinedRuleStrategy,
)


def _make_spec(
    *,
    name: str = "structured_mined_h1_buy_3",
    direction: str = "buy",
    timeframe: str = "H1",
    conditions=None,
    barrier=None,
) -> MinedRuleSpec:
    if conditions is None:
        conditions = (
            MinedRuleCondition(
                indicator="adx14", field="minus_di", op="<=", threshold=19.38
            ),
            MinedRuleCondition(
                indicator="regime_transition",
                field="bars_in_regime",
                op="<=",
                threshold=24.5,
            ),
            MinedRuleCondition(
                indicator="stoch_rsi14",
                field="stoch_rsi_k",
                op=">",
                threshold=23.12,
            ),
        )
    if barrier is None:
        barrier = MinedRuleBarrier(sl_atr=2.0, tp_atr=3.0, time_bars=80)
    return MinedRuleSpec(
        name=name,
        direction=direction,
        timeframe=timeframe,
        conditions=tuple(conditions),
        barrier=barrier,
        train_wr=0.639,
        test_wr=0.528,
        train_n=696,
        test_n=301,
        barrier_wr=0.559,
    )


def _ctx(indicators: Dict[str, Dict[str, float]]) -> SignalContext:
    return SignalContext(
        symbol="XAUUSD",
        timeframe="H1",
        strategy="test",
        indicators=indicators,
        metadata={},
        htf_indicators={},
    )


# ── Spec 不可变 + 类型 ───────────────────────────────────────────────


def test_spec_is_frozen() -> None:
    spec = _make_spec()
    with pytest.raises(Exception):
        spec.name = "modified"  # frozen → AttributeError / FrozenInstanceError


def test_condition_op_validation() -> None:
    """condition.op 必须是合法操作符。"""
    with pytest.raises(ValueError):
        MinedRuleCondition(indicator="adx14", field="adx", op="!!", threshold=10.0)


# ── MinedRuleStrategy: 接口契约 ──────────────────────────────────────


def test_strategy_inherits_structured_base() -> None:
    spec = _make_spec()
    strat = MinedRuleStrategy(spec)
    assert isinstance(strat, StructuredStrategyBase)


def test_strategy_name_from_spec() -> None:
    spec = _make_spec(name="structured_mined_m30_sell_5")
    strat = MinedRuleStrategy(spec)
    assert strat.name == "structured_mined_m30_sell_5"


def test_required_indicators_extracted_from_conditions() -> None:
    """conditions 中所有 indicator + atr14（barrier 计算用）→ required_indicators。"""
    spec = _make_spec()
    strat = MinedRuleStrategy(spec)
    expected = {"adx14", "regime_transition", "stoch_rsi14", "atr14"}
    assert set(strat.required_indicators) == expected


def test_strategy_category_and_htf_policy_defaults() -> None:
    strat = MinedRuleStrategy(_make_spec())
    assert strat.category == "mined_rule"
    assert strat.htf_policy == HtfPolicy.NONE


# ── _why: conditions 评估 ────────────────────────────────────────────


def test_why_passes_when_all_conditions_met() -> None:
    """所有 conditions 满足 → 返回 spec.direction + valid。"""
    spec = _make_spec()
    strat = MinedRuleStrategy(spec)
    ctx = _ctx(
        {
            "adx14": {"minus_di": 18.0},
            "regime_transition": {"bars_in_regime": 20.0},
            "stoch_rsi14": {"stoch_rsi_k": 25.0},
        }
    )

    valid, direction, score, reason = strat._why(ctx)

    assert valid is True
    assert direction == "buy"
    assert score > 0
    assert "all_conditions_met" in reason or "matched" in reason


def test_why_blocks_when_le_condition_fails() -> None:
    """<= 条件失败 → 返回 invalid。"""
    spec = _make_spec()
    strat = MinedRuleStrategy(spec)
    ctx = _ctx(
        {
            "adx14": {"minus_di": 25.0},  # > 19.38 → fail
            "regime_transition": {"bars_in_regime": 20.0},
            "stoch_rsi14": {"stoch_rsi_k": 25.0},
        }
    )

    valid, direction, _, reason = strat._why(ctx)

    assert valid is False
    assert direction is None
    assert "minus_di" in reason


def test_why_blocks_when_gt_condition_fails() -> None:
    """> 条件失败 → 返回 invalid。"""
    spec = _make_spec()
    strat = MinedRuleStrategy(spec)
    ctx = _ctx(
        {
            "adx14": {"minus_di": 18.0},
            "regime_transition": {"bars_in_regime": 20.0},
            "stoch_rsi14": {"stoch_rsi_k": 20.0},  # <= 23.12 → fail
        }
    )

    valid, _, _, reason = strat._why(ctx)

    assert valid is False
    assert "stoch_rsi_k" in reason


def test_why_blocks_when_indicator_missing() -> None:
    """所需 indicator 缺失 → 返回 invalid（warmup safe）。"""
    spec = _make_spec()
    strat = MinedRuleStrategy(spec)
    ctx = _ctx(
        {
            # adx14 缺失
            "regime_transition": {"bars_in_regime": 20.0},
            "stoch_rsi14": {"stoch_rsi_k": 25.0},
        }
    )

    valid, _, _, reason = strat._why(ctx)

    assert valid is False
    assert "missing" in reason or "adx14" in reason


def test_why_blocks_when_field_missing() -> None:
    spec = _make_spec()
    strat = MinedRuleStrategy(spec)
    ctx = _ctx(
        {
            "adx14": {},  # minus_di 字段缺失
            "regime_transition": {"bars_in_regime": 20.0},
            "stoch_rsi14": {"stoch_rsi_k": 25.0},
        }
    )

    valid, _, _, reason = strat._why(ctx)

    assert valid is False
    assert "minus_di" in reason


def test_why_supports_sell_direction() -> None:
    spec = _make_spec(name="mined_h1_sell_2", direction="sell")
    strat = MinedRuleStrategy(spec)
    ctx = _ctx(
        {
            "adx14": {"minus_di": 18.0},
            "regime_transition": {"bars_in_regime": 20.0},
            "stoch_rsi14": {"stoch_rsi_k": 25.0},
        }
    )

    valid, direction, _, _ = strat._why(ctx)

    assert valid is True
    assert direction == "sell"


# ── 各种 op 评估 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "op, value, threshold, expected",
    [
        (">", 25.0, 20.0, True),
        (">", 20.0, 20.0, False),
        (">=", 20.0, 20.0, True),
        ("<", 15.0, 20.0, True),
        ("<", 20.0, 20.0, False),
        ("<=", 20.0, 20.0, True),
        ("==", 20.0, 20.0, True),
        ("!=", 25.0, 20.0, True),
    ],
)
def test_condition_op_evaluators(op, value, threshold, expected) -> None:
    spec = _make_spec(
        conditions=(
            MinedRuleCondition(indicator="x", field="v", op=op, threshold=threshold),
        ),
    )
    strat = MinedRuleStrategy(spec)
    ctx = _ctx({"x": {"v": value}})

    valid, _, _, _ = strat._why(ctx)

    assert valid is expected


# ── _when 始终 valid（mined rules 已含触发条件）──────────────────────


def test_when_always_passes_for_mined_rules() -> None:
    """_when 不再做额外门控——conditions 已涵盖入场触发。"""
    strat = MinedRuleStrategy(_make_spec())
    ctx = _ctx({})

    valid, score, _ = strat._when(ctx, direction="buy")

    assert valid is True
    assert score > 0


# ── _entry_spec / _exit_spec ──────────────────────────────────────────


def test_entry_spec_is_market() -> None:
    """mined rules 默认市价入场（barrier 已锁 SL/TP）。"""
    strat = MinedRuleStrategy(_make_spec())
    ctx = _ctx({})

    es = strat._entry_spec(ctx, direction="buy")

    assert es.entry_type == EntryType.MARKET


def test_exit_spec_barrier_from_spec() -> None:
    """exit_spec 复用 spec.barrier（mining 已识别的最佳 SL/TP/Time）。"""
    spec = _make_spec(barrier=MinedRuleBarrier(sl_atr=1.5, tp_atr=2.0, time_bars=20))
    strat = MinedRuleStrategy(spec)
    ctx = _ctx({})

    xs = strat._exit_spec(ctx, direction="buy")

    assert xs.mode == ExitMode.BARRIER
    assert xs.sl_atr == 1.5
    assert xs.tp_atr == 2.0
    assert xs.time_bars == 20


# ── Provenance / 追溯 ────────────────────────────────────────────────


def test_research_provenance_refs_set_from_spec() -> None:
    """spec.mining_run_id 应反映到 research_provenance_refs（合规追溯）。"""
    spec = _make_spec()
    spec_with_runid = MinedRuleSpec(
        name=spec.name,
        direction=spec.direction,
        timeframe=spec.timeframe,
        conditions=spec.conditions,
        barrier=spec.barrier,
        mining_run_id="mine_xxx_2026-04-28",
        train_wr=spec.train_wr,
        test_wr=spec.test_wr,
        train_n=spec.train_n,
        test_n=spec.test_n,
        barrier_wr=spec.barrier_wr,
    )
    strat = MinedRuleStrategy(spec_with_runid)

    assert "mine_xxx_2026-04-28" in strat.research_provenance_refs
