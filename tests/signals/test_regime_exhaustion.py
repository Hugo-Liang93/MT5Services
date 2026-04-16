"""StructuredRegimeExhaustion 单元测试 — 覆盖四种 Regime + 边界条件。"""

from __future__ import annotations

import pytest

from src.signals.evaluation.regime import RegimeType
from src.signals.models import SignalContext
from src.signals.strategies.structured.regime_exhaustion import (
    StructuredRegimeExhaustion,
)


def _make_context(
    *,
    adx: float = 65.0,
    adx_d3: float = -2.0,
    plus_di: float = 35.0,
    minus_di: float = 20.0,
    rsi: float = 55.0,
    rsi_d3: float = 0.0,
    stoch_k: float = 80.0,
    stoch_d: float = 75.0,
    atr: float = 20.0,
    volume_ratio: float = 1.0,
    regime: str = "trending",
    compression_state: str = "none",
    breakout_state: str = "none",
) -> SignalContext:
    """构造测试用 SignalContext。"""
    return SignalContext(
        symbol="XAUUSD",
        timeframe="H1",
        strategy="structured_regime_exhaustion",
        indicators={
            "adx14": {
                "adx": adx,
                "adx_d3": adx_d3,
                "plus_di": plus_di,
                "minus_di": minus_di,
            },
            "rsi14": {"rsi": rsi, "rsi_d3": rsi_d3},
            "stoch_rsi14": {"stoch_rsi_k": stoch_k, "stoch_rsi_d": stoch_d},
            "atr14": {"atr": atr},
            "bar_stats20": {
                "close_position": 0.5,
                "body_ratio": 1.0,
                "range_ratio": 1.0,
            },
            "volume_ratio20": {"volume_ratio": volume_ratio},
        },
        metadata={
            "_regime": regime,
            "market_structure": {
                "compression_state": compression_state,
                "breakout_state": breakout_state,
            },
        },
        htf_indicators={},
    )


class TestStructuredRegimeExhaustion:
    """StructuredRegimeExhaustion 测试。"""

    def setup_method(self) -> None:
        self.strategy = StructuredRegimeExhaustion()

    # ── 卖出信号（多头耗竭） ──

    def test_sell_signal_bullish_exhaustion(self) -> None:
        """TRENDING + ADX=65 + adx_d3=-2 + plus_di > minus_di → SELL。"""
        ctx = _make_context(
            adx=65.0,
            adx_d3=-2.0,
            plus_di=35.0,
            minus_di=20.0,
            rsi=55.0,
            stoch_k=80.0,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "sell"
        assert decision.confidence > 0.0

    # ── 买入信号（空头耗竭） ──

    def test_buy_signal_bearish_exhaustion(self) -> None:
        """TRENDING + ADX=65 + adx_d3=-2 + minus_di > plus_di → BUY。"""
        ctx = _make_context(
            adx=65.0,
            adx_d3=-2.0,
            plus_di=20.0,
            minus_di=35.0,
            rsi=45.0,
            stoch_k=20.0,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "buy"
        assert decision.confidence > 0.0

    # ── Regime 过滤 ──

    def test_ranging_regime_affinity_zero(self) -> None:
        """RANGING regime_affinity=0.0 → 策略仍可评估，runtime 层乘 0.0 过滤。"""
        # regime_affinity 由 SignalRuntime 在 evaluate 后应用，策略本身不过滤
        assert self.strategy.regime_affinity[RegimeType.RANGING] == 0.0

    # ── ADX 门控 ──

    def test_adx_too_low_rejected(self) -> None:
        """ADX=30 < 55 → why 门控拒绝。"""
        ctx = _make_context(adx=30.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_adx_still_rising_rejected(self) -> None:
        """ADX=65 but adx_d3=+3（仍在上升）→ why 门控拒绝。"""
        ctx = _make_context(adx=65.0, adx_d3=3.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    # ── StochRSI 门控 ──

    def test_stoch_not_overbought_sell_rejected(self) -> None:
        """卖出时 StochRSI_k=50 < 70 → when 门控拒绝。"""
        ctx = _make_context(
            adx=65.0,
            adx_d3=-2.0,
            plus_di=35.0,
            minus_di=20.0,
            stoch_k=50.0,
            rsi=55.0,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_stoch_not_oversold_buy_rejected(self) -> None:
        """买入时 StochRSI_k=50 > 30 → when 门控拒绝。"""
        ctx = _make_context(
            adx=65.0,
            adx_d3=-2.0,
            plus_di=20.0,
            minus_di=35.0,
            stoch_k=50.0,
            rsi=45.0,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    # ── RSI 范围门控 ──

    def test_rsi_too_high_for_sell_rejected(self) -> None:
        """卖出时 RSI=75 > 65 → when RSI 范围门控拒绝。"""
        ctx = _make_context(
            adx=65.0,
            adx_d3=-2.0,
            plus_di=35.0,
            minus_di=20.0,
            stoch_k=80.0,
            rsi=75.0,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_rsi_too_low_for_sell_rejected(self) -> None:
        """卖出时 RSI=30 < 45 → when RSI 范围门控拒绝。"""
        ctx = _make_context(
            adx=65.0,
            adx_d3=-2.0,
            plus_di=35.0,
            minus_di=20.0,
            stoch_k=80.0,
            rsi=30.0,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    # ── 评分梯度 ──

    def test_higher_adx_higher_confidence(self) -> None:
        """ADX=70 比 ADX=56 信号更强（why_score 更高）。"""
        ctx_high = _make_context(adx=70.0, adx_d3=-2.0, stoch_k=85.0, rsi=55.0)
        ctx_low = _make_context(adx=56.0, adx_d3=-2.0, stoch_k=85.0, rsi=55.0)
        d_high = self.strategy.evaluate(ctx_high)
        d_low = self.strategy.evaluate(ctx_low)
        assert d_high.direction == "sell"
        assert d_low.direction == "sell"
        assert d_high.confidence > d_low.confidence

    # ── 结构位加分 ──

    def test_compression_state_bonus(self) -> None:
        """compression_state 非 none → where 加分。"""
        ctx_with = _make_context(
            adx=65.0, adx_d3=-2.0, stoch_k=80.0, rsi=55.0,
            compression_state="active",
        )
        ctx_without = _make_context(
            adx=65.0, adx_d3=-2.0, stoch_k=80.0, rsi=55.0,
        )
        d_with = self.strategy.evaluate(ctx_with)
        d_without = self.strategy.evaluate(ctx_without)
        assert d_with.direction == "sell"
        assert d_without.direction == "sell"
        assert d_with.confidence > d_without.confidence

    # ── Volume 加分 ──

    def test_volume_bonus(self) -> None:
        """volume_ratio > 1.2 → volume 加分。"""
        ctx_vol = _make_context(
            adx=65.0, adx_d3=-2.0, stoch_k=80.0, rsi=55.0,
            volume_ratio=1.5,
        )
        ctx_no_vol = _make_context(
            adx=65.0, adx_d3=-2.0, stoch_k=80.0, rsi=55.0,
            volume_ratio=0.8,
        )
        d_vol = self.strategy.evaluate(ctx_vol)
        d_no_vol = self.strategy.evaluate(ctx_no_vol)
        assert d_vol.direction == "sell"
        assert d_no_vol.direction == "sell"
        assert d_vol.confidence > d_no_vol.confidence

    # ── Exit spec ──

    def test_exit_spec_barrier_mode(self) -> None:
        """出场规格：SL=1.5 ATR, TP=2.0 ATR, time_bars=20。"""
        ctx = _make_context(adx=65.0, adx_d3=-2.0, stoch_k=80.0, rsi=55.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "sell"
        exit_spec = decision.metadata.get("exit_spec", {})
        assert exit_spec.get("sl_atr") == 1.5
        assert exit_spec.get("tp_atr") == 2.0
        assert exit_spec.get("time_bars") == 20

    # ── 缺失数据 ──

    def test_no_adx_data_rejected(self) -> None:
        """缺失 ADX 数据 → hold。"""
        ctx = _make_context()
        ctx.indicators["adx14"] = {}
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_no_stoch_rsi_rejected(self) -> None:
        """缺失 StochRSI 数据 → hold。"""
        ctx = _make_context(adx=65.0, adx_d3=-2.0, rsi=55.0)
        ctx.indicators["stoch_rsi14"] = {}
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"
