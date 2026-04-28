"""broker tick 关闭后从 close_price 推断 exit_reason 的 helper 测试。

2026-04-28 demo-main 观测：broker 在 tick 层触发 SL 后 reconciliation 标
`pos.close_source = history_deals` 但**未**设 `pos.last_exit_reason`，导致
trade_outcomes.metadata 缺 `exit_reason` 字段，外部无法区分 stop_loss /
take_profit / 其它 broker 关闭。
"""

from __future__ import annotations

from src.trading.positions.reconciliation import infer_exit_reason_from_price
from src.trading.reasons import (
    REASON_BROKER_CLOSE,
    REASON_STOP_LOSS,
    REASON_TAKE_PROFIT,
)


def _pos(
    *,
    action: str = "buy",
    entry_price: float = 4678.71,
    current_stop_loss: float = 4666.30,
    current_take_profit: float = 4696.04,
    atr_at_entry: float = 6.20,
):
    """构造最小 TrackedPosition-like 对象。"""

    class _P:
        pass

    p = _P()
    p.action = action
    p.entry_price = entry_price
    p.current_stop_loss = current_stop_loss
    p.current_take_profit = current_take_profit
    p.atr_at_entry = atr_at_entry
    return p


def test_close_at_stop_loss_inferred() -> None:
    """复现 demo trade 280107210: buy entry=4678.71 sl=4666.30 close=4667.58
    → 容差内 (0.1×ATR=0.62)，应推断为 stop_loss。"""
    pos = _pos()
    assert infer_exit_reason_from_price(pos, close_price=4667.58) == REASON_STOP_LOSS


def test_close_at_take_profit_inferred() -> None:
    """close 等于 current_take_profit → take_profit。"""
    pos = _pos()
    assert infer_exit_reason_from_price(pos, close_price=4696.10) == REASON_TAKE_PROFIT


def test_close_far_from_sl_tp_falls_back_to_broker_close() -> None:
    """close 既不在 SL 也不在 TP 容差内 → broker_close（manual / margin / 未知）。"""
    pos = _pos()
    # entry=4678.71, mid 区间，远离 SL 4666 和 TP 4696
    assert infer_exit_reason_from_price(pos, close_price=4682.50) == REASON_BROKER_CLOSE


def test_close_price_none_returns_broker_close() -> None:
    """close_price 缺失（MT5 missing）→ broker_close。"""
    pos = _pos()
    assert infer_exit_reason_from_price(pos, close_price=None) == REASON_BROKER_CLOSE


def test_sl_missing_falls_through_to_tp() -> None:
    """current_stop_loss 为 None（极少见）但 close 在 TP 附近 → take_profit。"""
    pos = _pos(current_stop_loss=None)
    assert infer_exit_reason_from_price(pos, close_price=4696.04) == REASON_TAKE_PROFIT


def test_both_sl_tp_missing_returns_broker_close() -> None:
    """SL/TP 都缺失 → 无从推断 → broker_close。"""
    pos = _pos(current_stop_loss=None, current_take_profit=None)
    assert infer_exit_reason_from_price(pos, close_price=4680.0) == REASON_BROKER_CLOSE


def test_tolerance_scales_with_atr() -> None:
    """容差 = max(atr_at_entry × 0.1, 0.5)。
    ATR=20 (高波动 XAUUSD H4) → 容差 2.0；ATR=2 (M5 低波动) → 容差 0.5 floor。"""
    # 高 ATR：close 与 SL 差 1.8 USD < 2.0 容差 → SL
    pos_high_atr = _pos(atr_at_entry=20.0)
    assert (
        infer_exit_reason_from_price(pos_high_atr, close_price=4668.10)
        == REASON_STOP_LOSS
    )
    # 低 ATR：close 与 SL 差 1.8 > 0.5 容差 → 落到 broker_close
    pos_low_atr = _pos(atr_at_entry=2.0)
    assert (
        infer_exit_reason_from_price(pos_low_atr, close_price=4668.10)
        == REASON_BROKER_CLOSE
    )


def test_sell_position_sl_above_entry() -> None:
    """sell 仓 SL > entry：close 4691.0 vs sl 4691.5 → stop_loss。"""
    pos = _pos(
        action="sell",
        entry_price=4670.73,
        current_stop_loss=4691.50,
        current_take_profit=4649.73,
    )
    assert infer_exit_reason_from_price(pos, close_price=4691.00) == REASON_STOP_LOSS
