"""F-12b Plan B: ExitSpec mode=barrier 分派 + evaluate_barrier_exit 行为。

覆盖：
- ExitSpec BARRIER 模式强制校验必填字段
- evaluate_barrier_exit LONG TP/SL/Time 三条 barrier 独立命中
- evaluate_barrier_exit SHORT 镜像语义
- evaluate_exit 按 mode 分派：barrier 走 barrier 路径，chandelier 走 Chandelier
- Chandelier 默认路径向后兼容（未声明 mode / CHANDELIER 模式）
"""

from __future__ import annotations

import pytest

from src.signals.strategies.structured.base import ExitMode, ExitSpec
from src.trading.positions.exit_rules import (
    ChandelierConfig,
    ExitCheckResult,
    evaluate_barrier_exit,
    evaluate_exit,
)
from src.trading.reasons import (
    REASON_STOP_LOSS,
    REASON_TAKE_PROFIT,
    REASON_TIMEOUT,
    REASON_TRAILING_STOP,
)


# ── ExitSpec 校验 ──────────────────────────────────────────────────────


def test_exit_spec_barrier_requires_sl_tp_time():
    # Barrier 模式必须三个字段齐全
    with pytest.raises(ValueError, match="sl_atr"):
        ExitSpec(mode=ExitMode.BARRIER, tp_atr=2.0, time_bars=20)
    with pytest.raises(ValueError, match="tp_atr"):
        ExitSpec(mode=ExitMode.BARRIER, sl_atr=1.0, time_bars=20)
    with pytest.raises(ValueError, match="time_bars"):
        ExitSpec(mode=ExitMode.BARRIER, sl_atr=1.0, tp_atr=2.0)
    # 零值也拒绝
    with pytest.raises(ValueError):
        ExitSpec(mode=ExitMode.BARRIER, sl_atr=0.0, tp_atr=2.0, time_bars=20)


def test_exit_spec_barrier_valid():
    spec = ExitSpec(mode=ExitMode.BARRIER, sl_atr=1.5, tp_atr=3.0, time_bars=20)
    d = spec.to_dict()
    assert d["mode"] == "barrier"
    assert d["sl_atr"] == 1.5
    assert d["tp_atr"] == 3.0
    assert d["time_bars"] == 20


def test_exit_spec_chandelier_default_backward_compat():
    spec = ExitSpec(aggression=0.65)  # 老代码调法
    d = spec.to_dict()
    assert d["mode"] == "chandelier"
    assert d["aggression"] == 0.65
    assert d["sl_atr"] is None
    assert d["tp_atr"] is None
    assert d["time_bars"] is None


# ── evaluate_barrier_exit 核心行为 ─────────────────────────────────────


def _make_result_args(
    action="buy", entry=100.0, high=100.5, low=99.5, close=100.0,
    atr_at_entry=1.0, sl_atr=1.0, tp_atr=2.0, time_bars=20,
    bars_held=1, initial_risk=1.0,
):
    return dict(
        action=action, entry_price=entry, bar_high=high, bar_low=low,
        bar_close=close, atr_at_entry=atr_at_entry, sl_atr=sl_atr,
        tp_atr=tp_atr, time_bars=time_bars, bars_held=bars_held,
        initial_risk=initial_risk,
    )


def test_barrier_long_tp_hit():
    # entry=100, ATR=1, TP=2×ATR → 102；bar_high=102.5 命中
    result = evaluate_barrier_exit(**_make_result_args(high=102.5, low=99.8))
    assert result.should_close is True
    assert result.close_reason == REASON_TAKE_PROFIT
    assert result.new_stop_loss == 102.0  # tp_price


def test_barrier_long_sl_hit():
    # entry=100, SL=1×ATR → 99；bar_low=98 命中
    result = evaluate_barrier_exit(**_make_result_args(high=100.2, low=98.0))
    assert result.should_close is True
    assert result.close_reason == REASON_STOP_LOSS
    assert result.new_stop_loss == 99.0


def test_barrier_long_sl_wins_when_both_hit_same_bar():
    # 同一 bar 同时触 SL 和 TP → 保守取 SL（悲观假设）
    result = evaluate_barrier_exit(**_make_result_args(high=103.0, low=98.0))
    assert result.close_reason == REASON_STOP_LOSS


def test_barrier_long_time_barrier():
    # bars_held >= time_bars，价格在范围内
    result = evaluate_barrier_exit(
        **_make_result_args(high=101.0, low=99.5, bars_held=20, time_bars=20)
    )
    assert result.should_close is True
    assert result.close_reason == REASON_TIMEOUT


def test_barrier_long_no_exit():
    result = evaluate_barrier_exit(**_make_result_args(high=101.0, low=99.5))
    assert result.should_close is False
    assert result.close_reason == ""


def test_barrier_short_tp_hit():
    # SHORT entry=100, TP=2×ATR → 98；bar_low=97 命中
    result = evaluate_barrier_exit(
        **_make_result_args(action="sell", high=100.2, low=97.0)
    )
    assert result.should_close is True
    assert result.close_reason == REASON_TAKE_PROFIT
    assert result.new_stop_loss == 98.0


def test_barrier_short_sl_hit():
    # SHORT SL=1×ATR → 101；bar_high=102 命中
    result = evaluate_barrier_exit(
        **_make_result_args(action="sell", high=102.0, low=99.5)
    )
    assert result.close_reason == REASON_STOP_LOSS


def test_barrier_degraded_params_return_no_close():
    # sl_atr / tp_atr / atr_at_entry 为 0 → 退化，不 close（由 Chandelier 接管）
    result = evaluate_barrier_exit(**_make_result_args(atr_at_entry=0.0))
    assert result.should_close is False


# ── evaluate_exit 分派正确性 ─────────────────────────────────────────


def test_evaluate_exit_dispatches_to_barrier_when_mode_barrier():
    exit_spec = ExitSpec(
        mode=ExitMode.BARRIER, sl_atr=1.0, tp_atr=2.0, time_bars=20
    ).to_dict()
    result = evaluate_exit(
        action="buy",
        entry_price=100.0,
        bar_high=102.5,  # 触 TP barrier（1×ATR=1，TP=2.0 → 102.0）
        bar_low=99.5,
        bar_close=102.0,
        current_stop_loss=99.0,
        initial_risk=1.0,
        peak_price=102.0,
        current_atr=1.5,  # 当前 ATR（barrier 应忽略它）
        atr_at_entry=1.0,  # barrier 必须用 entry 时 ATR
        bars_held=5,
        config=ChandelierConfig(),
        exit_spec=exit_spec,
    )
    assert result.close_reason == REASON_TAKE_PROFIT


def test_evaluate_exit_chandelier_when_no_mode():
    # 老策略的 exit_spec 只有 aggression，走 Chandelier 路径
    exit_spec = ExitSpec(aggression=0.50).to_dict()
    result = evaluate_exit(
        action="buy",
        entry_price=100.0,
        bar_high=100.5,
        bar_low=99.8,
        bar_close=100.2,
        current_stop_loss=99.0,
        initial_risk=1.0,
        peak_price=100.5,
        current_atr=1.0,
        atr_at_entry=1.0,
        bars_held=5,
        config=ChandelierConfig(),
        exit_spec=exit_spec,
    )
    # Chandelier 在价格未异常时不 close（trailing 尚未触发）
    assert result.should_close is False


def test_evaluate_exit_chandelier_default_without_exit_spec():
    # 完全不传 exit_spec → 走 Chandelier 默认路径（向后兼容）
    result = evaluate_exit(
        action="buy",
        entry_price=100.0,
        bar_high=100.5,
        bar_low=99.8,
        bar_close=100.2,
        current_stop_loss=99.0,
        initial_risk=1.0,
        peak_price=100.5,
        current_atr=1.0,
        atr_at_entry=0.0,  # 旧代码可不传
        bars_held=5,
        config=ChandelierConfig(),
        exit_spec=None,
    )
    assert isinstance(result, ExitCheckResult)
    assert result.should_close is False
