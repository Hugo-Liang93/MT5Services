"""signal_exit 浮亏守卫测试。

2026-04-28 demo-main 观测：price_action 在 M30/M15 震荡市每 27-31 分钟被
signal_exit 主动平仓，r_multiple 范围 -0.62 ~ +0.05。这违反"SL 处理亏损"
first-principles —— signal_exit 应是"锁利早出"工具，不是"亏损止损"工具
（后者由 SL 负责）。

修复：evaluate_exit 中的 signal_exit 触发加 `r_multiple >= 0` 浮盈守卫。
浮亏状态下方向反转不触发主动平仓，让 SL 自然出场。
"""

from __future__ import annotations

from src.trading.positions.exit_rules import (
    REASON_SIGNAL_EXIT,
    ChandelierConfig,
    check_signal_reversal,
    evaluate_exit,
)


def _evaluate_for_reversal(
    action: str,
    bar_close: float,
    *,
    recent_signal_dirs: list[str],
    confirmation_bars: int = 2,
    entry_price: float = 100.0,
    initial_risk: float = 1.0,
):
    """构造已经触发 signal reversal 的最小 evaluate_exit 调用。

    SL 设到远离当前价确保不会被 trailing/hard_tp 抢先返回；
    peak_price 等于 bar_close 让 chandelier trailing 不出场。
    """
    if action == "buy":
        peak_price = max(entry_price, bar_close)
        current_sl = entry_price - 5.0  # 远离 → 不触 SL
    else:
        peak_price = min(entry_price, bar_close)
        current_sl = entry_price + 5.0

    return evaluate_exit(
        action=action,
        entry_price=entry_price,
        bar_high=bar_close + 0.05,
        bar_low=bar_close - 0.05,
        bar_close=bar_close,
        current_stop_loss=current_sl,
        initial_risk=initial_risk,
        peak_price=peak_price,
        current_atr=1.0,
        atr_at_entry=1.0,
        bars_held=5,
        recent_signal_dirs=recent_signal_dirs,
        config=ChandelierConfig(
            signal_exit_enabled=True,
            signal_exit_confirmation_bars=confirmation_bars,
        ),
    )


# ── check_signal_reversal pure 函数行为不变 ──────────────────────────


def test_check_signal_reversal_pure_function_unchanged() -> None:
    """check_signal_reversal 仍然只看方向，不感知 r_multiple（保持 pure semantic）。"""
    assert check_signal_reversal("buy", ["sell", "sell"], 2) is True
    assert check_signal_reversal("buy", ["sell", "buy"], 2) is False
    assert check_signal_reversal("sell", ["buy", "buy"], 2) is True
    assert check_signal_reversal("buy", [], 2) is False
    assert check_signal_reversal("buy", ["sell"], 2) is False  # 不足 N 根


# ── evaluate_exit 浮亏守卫 ──────────────────────────────────────────


def test_signal_exit_blocked_when_floating_loss_buy() -> None:
    """持仓 buy + 浮亏 (r=-0.5R) + 连续反向信号 → 不触发 signal_exit。"""
    result = _evaluate_for_reversal(
        action="buy",
        bar_close=99.5,  # entry=100 buy → r = -0.5R
        recent_signal_dirs=["sell", "sell"],
    )
    assert (
        result.close_reason != REASON_SIGNAL_EXIT
    ), "浮亏状态下 signal_exit 不应触发，应让 SL 处理"


def test_signal_exit_blocked_when_floating_loss_sell() -> None:
    """持仓 sell + 浮亏 (r=-0.5R) + 连续反向信号 → 不触发 signal_exit。"""
    result = _evaluate_for_reversal(
        action="sell",
        bar_close=100.5,  # entry=100 sell → r = -0.5R
        recent_signal_dirs=["buy", "buy"],
    )
    assert result.close_reason != REASON_SIGNAL_EXIT


def test_signal_exit_allowed_when_floating_profit_buy() -> None:
    """持仓 buy + 浮盈 (r=+0.5R) + 连续反向信号 → 触发 signal_exit（保留原行为）。"""
    result = _evaluate_for_reversal(
        action="buy",
        bar_close=100.5,  # entry=100 buy → r = +0.5R
        recent_signal_dirs=["sell", "sell"],
    )
    assert result.close_reason == REASON_SIGNAL_EXIT
    assert result.should_close is True


def test_signal_exit_allowed_at_breakeven() -> None:
    """r_multiple = 0（breakeven）+ 反向信号 → 触发 signal_exit（≥ 0 边界 inclusive）。"""
    result = _evaluate_for_reversal(
        action="buy",
        bar_close=100.0,  # r = 0
        recent_signal_dirs=["sell", "sell"],
    )
    assert result.close_reason == REASON_SIGNAL_EXIT


def test_no_signal_exit_when_no_reversal() -> None:
    """浮盈但反向信号不足 → 不触发（与原行为一致）。"""
    result = _evaluate_for_reversal(
        action="buy",
        bar_close=100.5,
        recent_signal_dirs=["sell", "buy"],  # 只有 1 根 sell
    )
    assert result.close_reason != REASON_SIGNAL_EXIT


def test_signal_exit_blocked_at_demo_observed_loss() -> None:
    """复现 demo 观测场景：r=-0.44R 浮亏不触发 signal_exit。

    2026-04-28 demo trade: structured_price_action sell, r_multiple=-0.4393,
    exit_reason=signal_exit. 修复后该场景应让 SL 处理。
    """
    result = _evaluate_for_reversal(
        action="sell",
        bar_close=100.4393,  # 模拟 r=-0.44R
        recent_signal_dirs=["buy", "buy"],
    )
    assert (
        result.close_reason != REASON_SIGNAL_EXIT
    ), "复现 04-28 demo 浮亏 signal_exit 场景：修复后应让 SL 处理"
