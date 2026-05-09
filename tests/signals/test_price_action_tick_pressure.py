"""T9: PA `_post_confidence_modifier` hook 测试 — tick_features 注入 PA confidence 修正。

ADR-014 + plan T9：当 SignalRuntime 把最近的 tick feature 注入到
ctx.indicators["tick_features"] 时，PA 应在顺向压力不足时降权 confidence。
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.signals.models import SignalContext
from src.signals.strategies.structured import StructuredPriceAction


def _make_context(
    *,
    tick_features: dict | None = None,
    candle: dict | None = None,
    bar_stats: dict | None = None,
    price_struct: dict | None = None,
    bb_position: float = 0.2,
    atr: float = 1.5,
    adx: float = 20.0,
) -> SignalContext:
    """构造 PA 能产生 buy 信号的最小上下文。"""
    indicators = {
        "candle_pattern": candle or {"pin_bar": 1.0, "engulfing": 0.0, "hammer": 0.0},
        "bar_stats20": bar_stats or {"body_ratio": 1.2, "close_position": 0.7},
        "price_struct20": price_struct or {"structure_type": 1.0, "trend_bars": 3.0},
        "atr14": {"atr": atr},
        "boll20": {
            "bb_upper": 110.0,
            "bb_lower": 90.0,
            "bb_middle": 100.0,
            "close": 90.0 + bb_position * 20.0,
        },
        "keltner20": {
            "kc_upper": 110.0,
            "kc_lower": 90.0,
            "kc_middle": 100.0,
        },
        "adx14": {"adx": adx, "plus_di": 25.0, "minus_di": 15.0},
    }
    if tick_features is not None:
        indicators["tick_features"] = tick_features
    return SignalContext(
        symbol="XAUUSD",
        timeframe="M15",
        strategy="structured_price_action",
        indicators=indicators,
        metadata={"bar_time": datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)},
    )


def test_pa_default_post_modifier_is_noop_without_tick_features() -> None:
    """无 tick_features → modifier=1.0，confidence 不变。"""
    pa = StructuredPriceAction()
    ctx = _make_context(tick_features=None)
    decision = pa.evaluate(ctx)
    assert decision.direction == "buy"
    assert decision.confidence > 0.5
    # trace 不应含 post_modifier
    trace_keys = [step[0] for step in decision.confidence_trace]
    assert "post_modifier" not in trace_keys


def test_pa_post_modifier_penalizes_when_buy_pressure_below_threshold() -> None:
    """direction=buy + buy_pressure < 0.55 → confidence × 0.7。"""
    pa = StructuredPriceAction()
    ctx_no_tick = _make_context(tick_features=None)
    ctx_low_pressure = _make_context(
        tick_features={"buy_pressure": 0.45, "sell_pressure": 0.55}
    )

    decision_no_tick = pa.evaluate(ctx_no_tick)
    decision_low_pressure = pa.evaluate(ctx_low_pressure)

    assert decision_no_tick.direction == "buy"
    assert decision_low_pressure.direction == "buy"
    # 降权后 confidence 严格小于无 tick 版本
    assert decision_low_pressure.confidence < decision_no_tick.confidence
    # 验证乘数（误差容忍 1e-6）
    expected = min(decision_no_tick.confidence * 0.7, 1.0)
    assert abs(decision_low_pressure.confidence - expected) < 1e-6
    # trace 必须含 post_modifier 步
    trace_keys = [step[0] for step in decision_low_pressure.confidence_trace]
    assert "post_modifier" in trace_keys


def test_pa_post_modifier_noop_when_buy_pressure_above_threshold() -> None:
    """direction=buy + buy_pressure ≥ 0.55 → modifier=1.0，confidence 不变。"""
    pa = StructuredPriceAction()
    ctx_no_tick = _make_context(tick_features=None)
    ctx_high_pressure = _make_context(
        tick_features={"buy_pressure": 0.65, "sell_pressure": 0.35}
    )

    decision_no_tick = pa.evaluate(ctx_no_tick)
    decision_high_pressure = pa.evaluate(ctx_high_pressure)

    assert abs(decision_no_tick.confidence - decision_high_pressure.confidence) < 1e-6


def test_pa_post_modifier_uses_sell_pressure_for_sell_direction() -> None:
    """direction=sell 时检查 sell_pressure（不是 buy_pressure）。"""
    pa = StructuredPriceAction()
    # 构造 sell 信号上下文
    ctx_low_sell_pressure = _make_context(
        candle={"pin_bar": -1.0, "engulfing": 0.0, "hammer": 0.0},
        bar_stats={"body_ratio": 1.2, "close_position": 0.3},
        price_struct={"structure_type": -1.0, "trend_bars": -3.0},
        bb_position=0.8,
        tick_features={
            "buy_pressure": 0.55,
            "sell_pressure": 0.45,
        },  # 顺向(sell) < 阈值
    )
    decision = pa.evaluate(ctx_low_sell_pressure)
    assert decision.direction == "sell"
    # 应被降权
    trace_keys = [step[0] for step in decision.confidence_trace]
    assert "post_modifier" in trace_keys


def test_pa_post_modifier_noop_when_pressure_field_missing() -> None:
    """tick_features 字典存在但缺 pressure 字段 → modifier=1.0。"""
    pa = StructuredPriceAction()
    ctx_no_tick = _make_context(tick_features=None)
    ctx_partial = _make_context(tick_features={"spread_points": 2.0})

    decision_no_tick = pa.evaluate(ctx_no_tick)
    decision_partial = pa.evaluate(ctx_partial)

    assert abs(decision_no_tick.confidence - decision_partial.confidence) < 1e-6
