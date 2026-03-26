"""tests/signals/test_regime.py — MarketRegimeDetector 单元测试。"""
from __future__ import annotations

import pytest

from src.signals.evaluation.regime import (
    MarketRegimeDetector,
    RegimeType,
    SoftRegimeResult,
)


# ── 测试数据工厂 ─────────────────────────────────────────────────────────────

def _indicators(
    *,
    adx: float | None = None,
    adx_d3: float | None = None,
    bb_upper: float | None = None,
    bb_lower: float | None = None,
    bb_mid: float | None = None,
    kc_upper: float | None = None,
    kc_lower: float | None = None,
) -> dict:
    result: dict = {}
    if adx is not None:
        adx_dict: dict[str, float] = {"adx": adx}
        if adx_d3 is not None:
            adx_dict["adx_d3"] = adx_d3
        result["adx14"] = adx_dict
    if bb_upper is not None or bb_lower is not None or bb_mid is not None:
        result["boll20"] = {
            "bb_upper": bb_upper,
            "bb_lower": bb_lower,
            "bb_mid": bb_mid,
        }
    if kc_upper is not None or kc_lower is not None:
        result["keltner20"] = {
            "kc_upper": kc_upper,
            "kc_lower": kc_lower,
        }
    return result


# ── TRENDING ─────────────────────────────────────────────────────────────────

class TestTrendingRegime:
    def test_adx_above_23_is_trending(self):
        d = MarketRegimeDetector()
        ind = _indicators(adx=30.0)
        assert d.detect(ind) == RegimeType.TRENDING

    def test_adx_exactly_23_is_trending(self):
        d = MarketRegimeDetector()
        ind = _indicators(adx=23.0)
        assert d.detect(ind) == RegimeType.TRENDING

    def test_adx_40_with_bb_data(self):
        d = MarketRegimeDetector()
        ind = _indicators(adx=40.0, bb_upper=2010.0, bb_lower=1990.0, bb_mid=2000.0)
        assert d.detect(ind) == RegimeType.TRENDING

    def test_custom_threshold(self):
        d = MarketRegimeDetector(adx_trending_threshold=30.0)
        assert d.detect(_indicators(adx=28.0)) != RegimeType.TRENDING
        assert d.detect(_indicators(adx=31.0)) == RegimeType.TRENDING


# ── RANGING ──────────────────────────────────────────────────────────────────

class TestRangingRegime:
    def test_adx_below_18_is_ranging(self):
        d = MarketRegimeDetector()
        ind = _indicators(adx=15.0, bb_upper=2010.0, bb_lower=1990.0, bb_mid=2000.0)
        assert d.detect(ind) == RegimeType.RANGING

    def test_adx_just_below_18(self):
        d = MarketRegimeDetector()
        ind = _indicators(adx=17.9, bb_upper=2020.0, bb_lower=1980.0, bb_mid=2000.0)
        assert d.detect(ind) == RegimeType.RANGING

    def test_no_bb_data_with_low_adx_is_ranging(self):
        d = MarketRegimeDetector()
        ind = _indicators(adx=10.0)
        assert d.detect(ind) == RegimeType.RANGING


# ── BREAKOUT ─────────────────────────────────────────────────────────────────

class TestBreakoutRegime:
    def test_kc_bb_squeeze_low_adx(self):
        """BB 完全在 KC 内 + ADX 低 → RANGING（盘整蓄力）。"""
        d = MarketRegimeDetector()
        ind = _indicators(
            adx=15.0,
            bb_upper=2005.0, bb_lower=1995.0, bb_mid=2000.0,
            kc_upper=2010.0, kc_lower=1990.0,
        )
        assert d.detect(ind) == RegimeType.RANGING

    def test_kc_bb_squeeze_mid_adx(self):
        """BB 完全在 KC 内 + ADX 中等 → BREAKOUT（突破前兆）。"""
        d = MarketRegimeDetector()
        ind = _indicators(
            adx=19.0,
            bb_upper=2005.0, bb_lower=1995.0, bb_mid=2000.0,
            kc_upper=2010.0, kc_lower=1990.0,
        )
        assert d.detect(ind) == RegimeType.BREAKOUT

    def test_kc_bb_squeeze_overrides_trending_adx(self):
        """即使 ADX≥25，Squeeze 优先级更高。"""
        d = MarketRegimeDetector()
        ind = _indicators(
            adx=28.0,
            bb_upper=2005.0, bb_lower=1995.0, bb_mid=2000.0,
            kc_upper=2010.0, kc_lower=1990.0,
        )
        assert d.detect(ind) == RegimeType.BREAKOUT

    def test_tight_bb_with_low_adx(self):
        """ADX<18 且 BB 极窄 → BREAKOUT（蓄力盘整）。"""
        d = MarketRegimeDetector()
        # bb_width_pct = (2001 - 1999) / 2000 = 0.001 < 0.008
        ind = _indicators(adx=12.0, bb_upper=2001.0, bb_lower=1999.0, bb_mid=2000.0)
        assert d.detect(ind) == RegimeType.BREAKOUT

    def test_relaxed_bb_threshold_marks_more_low_adx_setups_as_breakout(self):
        d = MarketRegimeDetector()
        # bb_width_pct = (2007 - 1993) / 2000 = 0.007 < 0.008
        ind = _indicators(adx=12.0, bb_upper=2007.0, bb_lower=1993.0, bb_mid=2000.0)
        assert d.detect(ind) == RegimeType.BREAKOUT

    def test_tight_bb_no_adx(self):
        d = MarketRegimeDetector()
        ind = _indicators(bb_upper=2001.0, bb_lower=1999.0, bb_mid=2000.0)
        assert d.detect(ind) == RegimeType.BREAKOUT

    def test_no_squeeze_when_bb_outside_kc(self):
        """BB 超出 KC 边界 → 不是 Squeeze。"""
        d = MarketRegimeDetector()
        ind = _indicators(
            adx=15.0,
            bb_upper=2015.0, bb_lower=1985.0, bb_mid=2000.0,
            kc_upper=2010.0, kc_lower=1990.0,
        )
        # ADX<18 且 BB 宽度 = 30/2000 = 1.5% > 0.8% → RANGING
        assert d.detect(ind) == RegimeType.RANGING


# ── UNCERTAIN ────────────────────────────────────────────────────────────────

class TestUncertainRegime:
    def test_no_indicators_is_uncertain(self):
        d = MarketRegimeDetector()
        assert d.detect({}) == RegimeType.UNCERTAIN

    def test_adx_in_transition_zone(self):
        d = MarketRegimeDetector()
        ind = _indicators(adx=20.0)
        assert d.detect(ind) == RegimeType.UNCERTAIN

    def test_adx_exactly_18(self):
        d = MarketRegimeDetector()
        ind = _indicators(adx=18.0)
        assert d.detect(ind) == RegimeType.UNCERTAIN

    def test_adx_just_below_23(self):
        d = MarketRegimeDetector()
        ind = _indicators(adx=22.9)
        assert d.detect(ind) == RegimeType.UNCERTAIN


# ── detect_with_detail ───────────────────────────────────────────────────────

class TestDetectWithDetail:
    def test_returns_regime_field(self):
        d = MarketRegimeDetector()
        result = d.detect_with_detail(_indicators(adx=30.0))
        assert result["regime"] == "trending"

    def test_returns_adx(self):
        d = MarketRegimeDetector()
        result = d.detect_with_detail(_indicators(adx=18.5))
        assert result["adx"] == pytest.approx(18.5)

    def test_squeeze_flag(self):
        d = MarketRegimeDetector()
        ind = _indicators(
            bb_upper=2005.0, bb_lower=1995.0, bb_mid=2000.0,
            kc_upper=2010.0, kc_lower=1990.0,
        )
        result = d.detect_with_detail(ind)
        assert result["is_kc_bb_squeeze"] is True

    def test_no_squeeze_when_missing_kc(self):
        d = MarketRegimeDetector()
        ind = _indicators(bb_upper=2005.0, bb_lower=1995.0, bb_mid=2000.0)
        result = d.detect_with_detail(ind)
        assert result["is_kc_bb_squeeze"] is None

    def test_bb_width_pct_calculated(self):
        d = MarketRegimeDetector()
        # (2020 - 1980) / 2000 = 0.02
        ind = _indicators(bb_upper=2020.0, bb_lower=1980.0, bb_mid=2000.0)
        result = d.detect_with_detail(ind)
        assert result["bb_width_pct"] == pytest.approx(0.02)

    def test_thresholds_in_output(self):
        d = MarketRegimeDetector(adx_trending_threshold=28.0, adx_ranging_threshold=18.0)
        result = d.detect_with_detail({})
        assert result["adx_trending_threshold"] == 28.0
        assert result["adx_ranging_threshold"] == 18.0

    def test_detect_with_detail_includes_soft_probabilities(self):
        d = MarketRegimeDetector()
        result = d.detect_with_detail(_indicators(adx=20.0))
        assert set(result["regime_probabilities"]) == {
            "trending",
            "ranging",
            "breakout",
            "uncertain",
        }
        assert result["soft_regime"] in result["regime_probabilities"]


class TestSoftRegime:
    def test_soft_regime_probabilities_sum_to_one(self):
        d = MarketRegimeDetector()
        result = d.detect_soft(_indicators(adx=20.0))
        assert isinstance(result, SoftRegimeResult)
        assert sum(result.probabilities.values()) == pytest.approx(1.0)

    def test_soft_regime_trending_dominates_high_adx(self):
        d = MarketRegimeDetector()
        result = d.detect_soft(_indicators(adx=32.0))
        assert result.dominant_regime == RegimeType.TRENDING
        assert result.probability(RegimeType.TRENDING) > result.probability(
            RegimeType.UNCERTAIN
        )

    def test_soft_regime_ranging_dominates_low_adx_wide_bb(self):
        d = MarketRegimeDetector()
        result = d.detect_soft(
            _indicators(adx=12.0, bb_upper=2025.0, bb_lower=1975.0, bb_mid=2000.0)
        )
        assert result.dominant_regime == RegimeType.RANGING

    def test_soft_regime_breakout_dominates_squeeze(self):
        d = MarketRegimeDetector()
        # ADX=22 (above ranging threshold) + squeeze → BREAKOUT dominant
        result = d.detect_soft(
            _indicators(
                adx=22.0,
                bb_upper=2005.0,
                bb_lower=1995.0,
                bb_mid=2000.0,
                kc_upper=2010.0,
                kc_lower=1990.0,
            )
        )
        assert result.dominant_regime == RegimeType.BREAKOUT
        assert result.is_kc_bb_squeeze is True

    def test_soft_regime_squeeze_low_adx_favors_ranging(self):
        """Squeeze + low ADX → RANGING dominant (consolidation)."""
        d = MarketRegimeDetector()
        result = d.detect_soft(
            _indicators(
                adx=13.0,
                bb_upper=2005.0,
                bb_lower=1995.0,
                bb_mid=2000.0,
                kc_upper=2010.0,
                kc_lower=1990.0,
            )
        )
        assert result.dominant_regime == RegimeType.RANGING
        assert result.is_kc_bb_squeeze is True

    def test_soft_regime_uncertain_dominates_transition_zone(self):
        d = MarketRegimeDetector()
        result = d.detect_soft(_indicators(adx=20.5))
        assert result.dominant_regime == RegimeType.UNCERTAIN

    def test_soft_regime_no_negative_probabilities_with_high_adx_delta(self):
        """High adx_d3 should not produce negative probabilities."""
        d = MarketRegimeDetector()
        result = d.detect_soft(_indicators(adx=30.0, adx_d3=8.0))
        for regime, prob in result.probabilities.items():
            assert prob >= 0.0, f"{regime} has negative probability {prob}"
        assert sum(result.probabilities.values()) == pytest.approx(1.0)

    def test_soft_regime_round_trips_via_dict(self):
        d = MarketRegimeDetector()
        result = d.detect_soft(_indicators(adx=21.0))
        rebuilt = SoftRegimeResult.from_dict(result.to_dict())
        assert rebuilt.dominant_regime == result.dominant_regime
        assert rebuilt.probability(RegimeType.UNCERTAIN) == pytest.approx(
            result.probability(RegimeType.UNCERTAIN)
        )


# ── 边界值 ───────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_none_adx_value_in_dict(self):
        """指标字典存在但 adx 字段为 None。"""
        d = MarketRegimeDetector()
        ind = {"adx14": {"adx": None}}
        assert d.detect(ind) == RegimeType.UNCERTAIN

    def test_non_numeric_adx_value(self):
        d = MarketRegimeDetector()
        ind = {"adx14": {"adx": "not_a_number"}}
        assert d.detect(ind) == RegimeType.UNCERTAIN

    def test_bb_mid_zero_does_not_crash(self):
        """bb_mid=0 时应避免除零错误。"""
        d = MarketRegimeDetector()
        ind = _indicators(adx=10.0, bb_upper=2001.0, bb_lower=1999.0, bb_mid=0.0)
        # bb_mid=0 → 无法计算宽度比例 → RANGING（adx<18）
        assert d.detect(ind) == RegimeType.RANGING

    def test_regime_type_is_string(self):
        """RegimeType 混入 str，可直接序列化。"""
        assert RegimeType.TRENDING == "trending"
        assert isinstance(RegimeType.TRENDING.value, str)
