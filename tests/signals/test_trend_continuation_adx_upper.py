"""StructuredTrendContinuation._htf_adx_upper 门控测试。

守护 2026-04-22 mining 发现：adx 高时 long 亏钱（M30 barrier IC=-0.229, sl hit 71%；
H1 mined rule adx <= 41.74）→ 新增 `_htf_adx_upper` 参数拒绝高 adx 进场。

测试点：
  - adx > upper → 拒绝，reason 标"htf_adx_high"
  - adx == upper → 接受（严格大于才拒）
  - adx < upper → 与旧行为一致（不被门控影响）
"""

from __future__ import annotations

from src.signals.models import SignalContext
from src.signals.strategies.structured.trend_continuation import (
    StructuredTrendContinuation,
)


def _ctx(adx: float, close: float = 3052.0, htf_ema: float = 3040.0) -> SignalContext:
    return SignalContext(
        symbol="XAUUSD",
        timeframe="H1",
        strategy="structured_trend_continuation",
        indicators={
            "rsi14": {"rsi": 42.0, "rsi_d3": -1.0},
            "atr14": {"atr": 18.0},
            "volume_ratio20": {"volume_ratio": 1.4},
            "donchian20": {"close": close},
        },
        metadata={
            "_regime": "trending",
            "market_structure": {
                "structure_bias": "bullish_pullback",
                "close_price": close,
            },
        },
        htf_indicators={
            "H4": {
                "supertrend14": {"direction": 1},
                "adx14": {"adx": adx},
                "ema50": {"ema": htf_ema},
            }
        },
    )


def _strategy() -> StructuredTrendContinuation:
    return StructuredTrendContinuation(
        name="structured_trend_continuation",
        htf="H4",
    )


class TestHtfAdxUpper:
    def test_adx_above_default_upper_rejected(self) -> None:
        """adx=60 > 默认 55 → 拒绝。"""
        strategy = _strategy()
        decision = strategy.evaluate(_ctx(adx=60.0))
        assert decision.direction == "hold"
        assert "htf_adx_high" in decision.reason
        assert "60" in decision.reason

    def test_adx_at_upper_boundary_accepted(self) -> None:
        """adx=55 == 默认 upper → 接受（严格大于才拒）。"""
        strategy = _strategy()
        decision = strategy.evaluate(_ctx(adx=55.0))
        # 边界通过后可能因其他门控（RSI/结构）hold，但 reason 不应是 adx_high
        assert "htf_adx_high" not in decision.reason

    def test_adx_below_upper_unaffected(self) -> None:
        """adx=28（正常 trending）< upper → 不受新门控影响。"""
        strategy = _strategy()
        decision = strategy.evaluate(_ctx(adx=28.0))
        assert "htf_adx_high" not in decision.reason

    def test_rejection_reason_shows_both_values(self) -> None:
        """reason 格式 'htf_adx_high:<current>>upper' 便于诊断。"""
        strategy = _strategy()
        decision = strategy.evaluate(_ctx(adx=70.0))
        assert "70" in decision.reason
        assert "55" in decision.reason  # 默认 upper
