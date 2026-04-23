"""StructuredMdiSell 单测。

守护 2026-04-22 mining M30 rule #1 的编码版本：
    adx14.minus_di > 20.56 AND macd.hist > -13.32 AND cci20.cci > -241.14 → sell

测试点：
  - 3 条硬门控各自拒绝（minus_di 弱 / macd 极端 / cci 极端）
  - 全部通过时 direction=sell + confidence > 0
  - HTF 上行冲突（SOFT_GATE）拒绝
"""

from __future__ import annotations

from src.signals.models import SignalContext
from src.signals.strategies.structured.mdi_sell import StructuredMdiSell


def _make_context(
    *,
    minus_di: float = 25.0,  # > 20.56 → OK
    plus_di: float = 15.0,
    adx: float = 28.0,
    macd_hist: float = 2.0,  # > -13.32 → OK
    cci: float = -80.0,  # > -241.14 → OK
    htf_direction: int = -1,
    htf_adx: float = 28.0,
) -> SignalContext:
    return SignalContext(
        symbol="XAUUSD",
        timeframe="M30",
        strategy="structured_mdi_sell",
        indicators={
            "adx14": {
                "adx": adx,
                "adx_d3": -1.0,
                "plus_di": plus_di,
                "minus_di": minus_di,
            },
            "macd": {"hist": macd_hist},
            "cci20": {"cci": cci},
            "atr14": {"atr": 18.0},
        },
        metadata={
            "_regime": "trending",
            "market_structure": {
                "structure_bias": "bearish_continuation",
                "close_price": 3020.0,
            },
        },
        htf_indicators={
            "H1": {
                "supertrend14": {"direction": htf_direction},
                "adx14": {"adx": htf_adx},
            }
        },
    )


class TestMdiSellGates:
    def test_full_accept_produces_sell_signal(self) -> None:
        """3 条硬门控全部通过 + HTF 对齐（下行）→ sell。"""
        decision = StructuredMdiSell().evaluate(_make_context())
        assert decision.direction == "sell"
        assert decision.confidence > 0.0
        assert "minus_di" in decision.reason or "macd" in decision.reason or decision.direction == "sell"

    def test_minus_di_below_threshold_rejected(self) -> None:
        """minus_di <= 20.56 → 拒绝（why 门控）。"""
        decision = StructuredMdiSell().evaluate(_make_context(minus_di=18.0))
        assert decision.direction == "hold"
        assert "minus_di_weak" in decision.reason

    def test_macd_hist_extreme_negative_rejected(self) -> None:
        """macd.hist <= -13.32 → 拒绝（when 门控，避免反弹临界）。"""
        decision = StructuredMdiSell().evaluate(_make_context(macd_hist=-15.0))
        assert decision.direction == "hold"
        assert "macd_extreme" in decision.reason

    def test_cci_extreme_oversold_rejected(self) -> None:
        """cci <= -241.14 → 拒绝（when 门控，避免超卖反弹临界）。"""
        decision = StructuredMdiSell().evaluate(_make_context(cci=-250.0))
        assert decision.direction == "hold"
        assert "cci_extreme" in decision.reason

    def test_htf_bullish_strong_trend_rejects_sell(self) -> None:
        """HTF 上行强趋势（adx>25 + direction=1）→ SOFT_GATE 拒绝 sell。"""
        decision = StructuredMdiSell().evaluate(
            _make_context(htf_direction=1, htf_adx=30.0)
        )
        assert decision.direction == "hold"
        assert "htf_bullish" in decision.reason

    def test_htf_weak_trend_does_not_block(self) -> None:
        """HTF 弱趋势（adx<=25）即使方向冲突也不 block（softgate only on strong trend）。"""
        decision = StructuredMdiSell().evaluate(
            _make_context(htf_direction=1, htf_adx=20.0)
        )
        # HTF 弱时不 block，其他门控通过 → sell
        assert decision.direction == "sell"

    def test_boundary_minus_di_just_above_threshold(self) -> None:
        """minus_di 恰好 > 20.56 边界（>= 20.57）→ 通过 why 门控。"""
        decision = StructuredMdiSell().evaluate(_make_context(minus_di=20.60))
        assert decision.direction == "sell"


class TestMdiSellBoundaries:
    def test_missing_minus_di_returns_no_data_reason(self) -> None:
        ctx = _make_context()
        ctx.indicators["adx14"]["minus_di"] = None
        decision = StructuredMdiSell().evaluate(ctx)
        assert decision.direction == "hold"
        assert "no_minus_di" in decision.reason

    def test_missing_macd_returns_no_data_reason(self) -> None:
        ctx = _make_context()
        ctx.indicators.pop("macd", None)
        decision = StructuredMdiSell().evaluate(ctx)
        assert decision.direction == "hold"
        assert "no_macd_hist" in decision.reason

    def test_missing_cci_returns_no_data_reason(self) -> None:
        ctx = _make_context()
        ctx.indicators.pop("cci20", None)
        decision = StructuredMdiSell().evaluate(ctx)
        assert decision.direction == "hold"
        assert "no_cci" in decision.reason


class TestMdiSellMetadata:
    def test_research_provenance_exposes_mining_source(self) -> None:
        decision = StructuredMdiSell().evaluate(_make_context())
        prov = decision.metadata.get("research_provenance", [])
        assert any("mined_rule.m30.2026-04-22" in p for p in prov)

    def test_entry_spec_is_market(self) -> None:
        decision = StructuredMdiSell().evaluate(_make_context())
        assert decision.metadata["entry_spec"]["entry_type"] == "market"
