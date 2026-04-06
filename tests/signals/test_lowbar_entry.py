"""StructuredLowbarEntry 单元测试 — 覆盖四种 Regime + 边界条件。"""

from __future__ import annotations

import pytest

from src.signals.evaluation.regime import RegimeType
from src.signals.models import SignalContext
from src.signals.strategies.structured.lowbar_entry import StructuredLowbarEntry


def _make_context(
    *,
    adx: float = 25.0,
    rsi: float = 45.0,
    rsi_d3: float = 0.0,
    close_position: float = 0.50,
    body_ratio: float = 1.0,
    atr: float = 20.0,
    regime: str = "ranging",
) -> SignalContext:
    """构造测试用 SignalContext。"""
    return SignalContext(
        symbol="XAUUSD",
        timeframe="H1",
        strategy="structured_lowbar_entry",
        indicators={
            "adx14": {"adx": adx, "adx_d3": 1.0, "plus_di": 20.0, "minus_di": 15.0},
            "rsi14": {"rsi": rsi, "rsi_d3": rsi_d3},
            "bar_stats20": {
                "close_position": close_position,
                "body_ratio": body_ratio,
                "range_ratio": 1.0,
                "is_bullish": 1.0 if close_position > 0.5 else -1.0,
            },
            "atr14": {"atr": atr},
        },
        metadata={"_regime": regime, "market_structure": {}},
        htf_indicators={},
    )


class TestStructuredLowbarEntry:
    """StructuredLowbarEntry 测试。"""

    def setup_method(self) -> None:
        self.strategy = StructuredLowbarEntry()

    # ── 买入信号 ──

    def test_buy_signal_low_close_position(self) -> None:
        """close_position <= 0.19 + ADX <= 36 + RSI <= 72.5 → buy。"""
        ctx = _make_context(adx=25.0, rsi=40.0, close_position=0.15)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "buy"
        assert decision.confidence > 0.0

    def test_buy_rsi_too_high_rejected(self) -> None:
        """RSI > 72.5 → when 门控拒绝。"""
        ctx = _make_context(adx=25.0, rsi=75.0, close_position=0.10)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_buy_adx_too_high_rejected(self) -> None:
        """ADX > 36 → why 门控拒绝。"""
        ctx = _make_context(adx=40.0, rsi=40.0, close_position=0.10)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_buy_rsi_d3_positive_bonus(self) -> None:
        """RSI d3 > 0（开始回升）→ 额外 bonus。"""
        ctx_rising = _make_context(adx=20.0, rsi=35.0, rsi_d3=2.0, close_position=0.15)
        ctx_flat = _make_context(adx=20.0, rsi=35.0, rsi_d3=0.0, close_position=0.15)
        d_rising = self.strategy.evaluate(ctx_rising)
        d_flat = self.strategy.evaluate(ctx_flat)
        assert d_rising.direction == "buy"
        assert d_flat.direction == "buy"
        assert d_rising.confidence > d_flat.confidence

    # ── 卖出信号 ──

    def test_sell_signal_high_close_position(self) -> None:
        """close_position >= 0.81 + ADX <= 36 + RSI >= 27.5 → sell。"""
        ctx = _make_context(adx=25.0, rsi=60.0, close_position=0.85)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "sell"
        assert decision.confidence > 0.0

    def test_sell_rsi_too_low_rejected(self) -> None:
        """RSI < 27.5 → when 门控拒绝。"""
        ctx = _make_context(adx=25.0, rsi=25.0, close_position=0.90)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    # ── 中间区域 ──

    def test_mid_close_position_hold(self) -> None:
        """close_position 在中间区域 → hold。"""
        ctx = _make_context(adx=25.0, rsi=50.0, close_position=0.50)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    # ── 四种 Regime 覆盖 ──

    def test_regime_ranging_affinity(self) -> None:
        """RANGING regime → affinity = 1.00。"""
        assert self.strategy.regime_affinity[RegimeType.RANGING] == 1.00

    def test_regime_trending_affinity(self) -> None:
        """TRENDING regime → affinity = 0.15（低，趋势市不适合反转）。"""
        assert self.strategy.regime_affinity[RegimeType.TRENDING] == 0.15

    def test_regime_breakout_affinity(self) -> None:
        """BREAKOUT regime → affinity = 0.20。"""
        assert self.strategy.regime_affinity[RegimeType.BREAKOUT] == 0.20

    def test_regime_uncertain_affinity(self) -> None:
        """UNCERTAIN regime → affinity = 0.70。"""
        assert self.strategy.regime_affinity[RegimeType.UNCERTAIN] == 0.70

    # ── 策略属性 ──

    def test_strategy_attributes(self) -> None:
        """验证策略基础属性。"""
        assert self.strategy.name == "structured_lowbar_entry"
        assert self.strategy.category == "reversion"
        assert "adx14" in self.strategy.required_indicators
        assert "rsi14" in self.strategy.required_indicators
        assert "bar_stats20" in self.strategy.required_indicators
        assert self.strategy.preferred_scopes == ("confirmed",)

    # ── 缺失数据 ──

    def test_missing_adx_hold(self) -> None:
        """缺少 ADX 指标 → hold。"""
        ctx = _make_context(close_position=0.10)
        ctx.indicators.pop("adx14")
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_missing_close_position_hold(self) -> None:
        """缺少 bar_stats20 → hold。"""
        ctx = _make_context(close_position=0.10)
        ctx.indicators.pop("bar_stats20")
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"
