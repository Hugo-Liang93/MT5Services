"""StructuredStrongTrendFollow 单元测试。

挖掘来源：2026-04-17 H1 rule_mining #5
  IF adx14.adx > 40.12 AND macd_fast.hist <= 1.61 AND roc12.roc > -1.17 THEN buy
"""

from __future__ import annotations

import pytest

from src.signals.evaluation.regime import RegimeType
from src.signals.models import SignalContext
from src.signals.strategies.structured.base import (
    ExitMode,
    HtfPolicy,
)
from src.signals.strategies.structured.strong_trend_follow import (
    StructuredStrongTrendFollow,
)


def _make_context(
    *,
    adx: float = 50.0,
    adx_d3: float = 1.0,
    plus_di: float = 35.0,
    minus_di: float = 15.0,
    macd_hist: float = 0.5,
    roc: float = 0.5,
    atr: float = 20.0,
    volume_ratio: float = 1.0,
    regime: str = "trending",
    compression_state: str = "none",
    breakout_state: str = "none",
) -> SignalContext:
    """构造测试用 SignalContext。默认所有条件满足 → buy。"""
    return SignalContext(
        symbol="XAUUSD",
        timeframe="H1",
        strategy="structured_strong_trend_follow",
        indicators={
            "adx14": {
                "adx": adx,
                "adx_d3": adx_d3,
                "plus_di": plus_di,
                "minus_di": minus_di,
            },
            "macd_fast": {"macd": 1.0, "signal": 0.5, "hist": macd_hist},
            "roc12": {"roc": roc},
            "atr14": {"atr": atr},
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


class TestClassAttributes:
    """类元数据验证。"""

    def setup_method(self) -> None:
        self.strategy = StructuredStrongTrendFollow()

    def test_name(self) -> None:
        assert self.strategy.name == "structured_strong_trend_follow"

    def test_category(self) -> None:
        assert self.strategy.category == "trend"

    def test_htf_policy_none(self) -> None:
        assert self.strategy.htf_policy == HtfPolicy.NONE

    def test_preferred_scopes_confirmed_only(self) -> None:
        assert self.strategy.preferred_scopes == ("confirmed",)

    def test_required_indicators(self) -> None:
        assert set(self.strategy.required_indicators) == {
            "atr14",
            "adx14",
            "macd_fast",
            "roc12",
            "volume_ratio20",
        }

    def test_regime_affinity_trending_primary(self) -> None:
        assert self.strategy.regime_affinity[RegimeType.TRENDING] == 1.00
        assert self.strategy.regime_affinity[RegimeType.BREAKOUT] == 0.60
        assert self.strategy.regime_affinity[RegimeType.RANGING] == 0.00
        assert self.strategy.regime_affinity[RegimeType.UNCERTAIN] == 0.20


class TestWhyGate:
    """_why() 硬门控测试。"""

    def setup_method(self) -> None:
        self.strategy = StructuredStrongTrendFollow()

    def test_adx_below_threshold_rejected(self) -> None:
        """ADX=30 < 40 → hold。"""
        ctx = _make_context(adx=30.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"
        assert "adx_low" in decision.reason

    def test_adx_at_threshold_rejected(self) -> None:
        """ADX=40 (=threshold) → hold（需严格 >）。"""
        ctx = _make_context(adx=40.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_di_not_bullish_rejected(self) -> None:
        """plus_di=20 <= minus_di=30 → hold（空头结构）。"""
        ctx = _make_context(plus_di=20.0, minus_di=30.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"
        assert "di_not_bullish" in decision.reason

    def test_di_equal_rejected(self) -> None:
        """plus_di = minus_di → hold（需严格 >）。"""
        ctx = _make_context(plus_di=25.0, minus_di=25.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_adx_d3_zero_rejected_mutex_with_exhaustion(self) -> None:
        """adx_d3=0 → hold（临界点归 regime_exhaustion）。"""
        ctx = _make_context(adx_d3=0.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"
        assert "adx_not_rising" in decision.reason

    def test_adx_d3_negative_rejected(self) -> None:
        """adx_d3=-1 → hold（趋势在减弱，让给 regime_exhaustion）。"""
        ctx = _make_context(adx_d3=-1.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_no_adx_data_rejected(self) -> None:
        """缺失 ADX 数据 → hold。"""
        ctx = _make_context()
        ctx.indicators["adx14"] = {}
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"
        assert "no_adx_data" in decision.reason

    def test_why_passes_all_bullish_strong_trend(self) -> None:
        """ADX=50 + plus_di>minus_di + adx_d3=1 + MACD/ROC pending → 先不 hold 于 why。

        但因 _when/_entry/_exit 还未实现，evaluate 会抛 NotImplementedError。
        仅验证 _why 本身逻辑：直接调用 _why() 方法。
        """
        ctx = _make_context(adx=50.0, plus_di=35.0, minus_di=15.0, adx_d3=1.0)
        ok, direction, score, reason = self.strategy._why(ctx)
        assert ok is True
        assert direction == "buy"
        assert 0.4 <= score <= 1.0
        assert "strong_trend" in reason


class TestWhenGate:
    """_when() 硬门控测试。"""

    def setup_method(self) -> None:
        self.strategy = StructuredStrongTrendFollow()

    def test_macd_hist_too_high_rejected(self) -> None:
        """macd_hist=2.0 > 1.61 → hold（动量过强，不符合回归中性条件）。"""
        ctx = _make_context(macd_hist=2.0)
        ok, score, reason = self.strategy._when(ctx, "buy")
        assert ok is False
        assert "macd_hist_too_high" in reason

    def test_macd_hist_too_low_rejected(self) -> None:
        """macd_hist=-3.0 < -2.0 → hold（深度崩盘保护）。"""
        ctx = _make_context(macd_hist=-3.0)
        ok, score, reason = self.strategy._when(ctx, "buy")
        assert ok is False
        assert "macd_hist_too_low" in reason

    def test_roc_too_low_rejected(self) -> None:
        """roc=-2.0 <= -1.17 → hold（动量已崩溃）。"""
        ctx = _make_context(roc=-2.0)
        ok, score, reason = self.strategy._when(ctx, "buy")
        assert ok is False
        assert "roc_too_low" in reason

    def test_no_macd_data_rejected(self) -> None:
        """缺失 macd_fast 数据 → hold。"""
        ctx = _make_context()
        ctx.indicators["macd_fast"] = {}
        ok, score, reason = self.strategy._when(ctx, "buy")
        assert ok is False
        assert "no_macd_or_roc" in reason

    def test_no_roc_data_rejected(self) -> None:
        """缺失 roc12 数据 → hold。"""
        ctx = _make_context()
        ctx.indicators["roc12"] = {}
        ok, score, reason = self.strategy._when(ctx, "buy")
        assert ok is False

    def test_macd_hist_zero_highest_score(self) -> None:
        """macd_hist=0 最接近中心 → score 最大（1.0）。"""
        ctx = _make_context(macd_hist=0.0, roc=0.5)
        ok, score, reason = self.strategy._when(ctx, "buy")
        assert ok is True
        assert score == pytest.approx(1.0, abs=0.05)

    def test_macd_hist_at_upper_bound_lowest_score(self) -> None:
        """macd_hist=1.61（上限）→ score 最低。"""
        ctx = _make_context(macd_hist=1.61, roc=0.5)
        ok, score, reason = self.strategy._when(ctx, "buy")
        assert ok is True
        # 距离 0 为 1.61，half_range=max(1.61, 2.0)=2.0，score = max(0, 1 - 1.61/2.0) = 0.195
        # 但下限 0.3，所以实际 0.3
        assert score >= 0.3

    def test_when_passes_typical_case(self) -> None:
        """典型入场：macd_hist=0.5, roc=0.5 → 通过。"""
        ctx = _make_context(macd_hist=0.5, roc=0.5)
        ok, score, reason = self.strategy._when(ctx, "buy")
        assert ok is True
        assert 0.3 <= score <= 1.0
        assert "timing" in reason


class TestWhereAndVolume:
    """软加分层测试。"""

    def setup_method(self) -> None:
        self.strategy = StructuredStrongTrendFollow()

    def test_where_default_zero(self) -> None:
        """无 compression/breakout → 0.0。"""
        ctx = _make_context()
        score, reason = self.strategy._where(ctx, "buy")
        assert score == 0.0

    def test_where_compression_bonus(self) -> None:
        """compression_state=active → 0.8 加分。"""
        ctx = _make_context(compression_state="active")
        score, reason = self.strategy._where(ctx, "buy")
        assert score == 0.8
        assert "compression" in reason

    def test_where_breakout_bonus(self) -> None:
        """breakout_state=bullish → 0.8 加分。"""
        ctx = _make_context(breakout_state="bullish")
        score, reason = self.strategy._where(ctx, "buy")
        assert score == 0.8
        assert "breakout" in reason

    def test_volume_bonus_low_volume_zero(self) -> None:
        """volume_ratio=0.9 < 1.2 → 0.0。"""
        ctx = _make_context(volume_ratio=0.9)
        bonus = self.strategy._volume_bonus(ctx, "buy")
        assert bonus == 0.0

    def test_volume_bonus_high_volume_full(self) -> None:
        """volume_ratio=1.5 >= 1.5 → 1.0。"""
        ctx = _make_context(volume_ratio=1.5)
        bonus = self.strategy._volume_bonus(ctx, "buy")
        assert bonus == pytest.approx(1.0, abs=0.01)

    def test_volume_bonus_mid_volume_partial(self) -> None:
        """volume_ratio=1.35（1.2 与 1.5 中间）→ 0.5。"""
        ctx = _make_context(volume_ratio=1.35)
        bonus = self.strategy._volume_bonus(ctx, "buy")
        assert bonus == pytest.approx(0.5, abs=0.01)


class TestEntryExitAndEvaluate:
    """入场/出场规格 + 端到端 evaluate 测试。"""

    def setup_method(self) -> None:
        self.strategy = StructuredStrongTrendFollow()

    def test_entry_spec_market(self) -> None:
        """入场：市价。"""
        ctx = _make_context()
        spec = self.strategy._entry_spec(ctx, "buy")
        assert spec.entry_type.value == "market"

    def test_exit_spec_barrier_mode(self) -> None:
        """出场：BARRIER (sl=1.5, tp=2.5, time=20)。"""
        ctx = _make_context()
        spec = self.strategy._exit_spec(ctx, "buy")
        assert spec.mode == ExitMode.BARRIER
        assert spec.sl_atr == 1.5
        assert spec.tp_atr == 2.5
        assert spec.time_bars == 20

    def test_evaluate_all_pass_returns_buy(self) -> None:
        """完整评估：所有门控通过 → buy 决策，confidence ≥ 0.56。"""
        ctx = _make_context(
            adx=50.0, adx_d3=1.0, plus_di=35.0, minus_di=15.0,
            macd_hist=0.5, roc=0.5, volume_ratio=1.3,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "buy"
        # base 0.50 + why ≥ 0.4×0.15=0.06 + when ≥ 0.3×0.15=0.045 → ≥ 0.605
        assert decision.confidence >= 0.56
        # meta 上应包含 entry_spec / exit_spec
        assert "entry_spec" in decision.metadata
        assert decision.metadata["exit_spec"]["mode"] == "barrier"

    def test_evaluate_signal_grade_a_with_all_bonus(self) -> None:
        """compression + high volume → where/vol 都加分 → grade=A。"""
        ctx = _make_context(
            adx=55.0, adx_d3=2.0, plus_di=40.0, minus_di=15.0,
            macd_hist=0.2, roc=0.8, volume_ratio=1.5,
            compression_state="active",
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "buy"
        assert decision.metadata["signal_grade"] == "A"

    def test_evaluate_confidence_capped_at_090(self) -> None:
        """极端强信号不超过 0.90 上限。"""
        ctx = _make_context(
            adx=80.0, adx_d3=5.0, plus_di=60.0, minus_di=10.0,
            macd_hist=0.0, roc=2.0, volume_ratio=2.0,
            compression_state="active",
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.confidence <= 0.90

    def test_evaluate_records_provenance(self) -> None:
        """metadata 记录 research_provenance。"""
        ctx = _make_context()
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "buy"
        provenance = decision.metadata.get("research_provenance", [])
        assert "2026-04-17-H1-rule-mining-#5" in provenance


class TestCatalogRegistration:
    """策略目录注册验证。"""

    def test_strategy_in_catalog(self) -> None:
        """catalog 中能找到 structured_strong_trend_follow。"""
        from src.signals.strategies.catalog import build_named_strategy_catalog

        catalog = build_named_strategy_catalog()
        assert "structured_strong_trend_follow" in catalog
        strategy = catalog["structured_strong_trend_follow"]
        assert isinstance(strategy, StructuredStrongTrendFollow)

    def test_strategy_exported_from_structured_module(self) -> None:
        """从 structured 模块能导入。"""
        from src.signals.strategies.structured import (
            StructuredStrongTrendFollow as ImportedClass,
        )
        assert ImportedClass is StructuredStrongTrendFollow
