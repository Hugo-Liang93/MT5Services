"""tests/signals/test_regime.py — MarketRegimeDetector 单元测试。"""
from __future__ import annotations

import pytest

from src.signals.regime import MarketRegimeDetector, RegimeType


# ── 测试数据工厂 ─────────────────────────────────────────────────────────────

def _indicators(
    *,
    adx: float | None = None,
    bb_upper: float | None = None,
    bb_lower: float | None = None,
    bb_mid: float | None = None,
    kc_upper: float | None = None,
    kc_lower: float | None = None,
) -> dict:
    result: dict = {}
    if adx is not None:
        result["adx14"] = {"adx": adx}
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
    def test_adx_above_25_is_trending(self):
        d = MarketRegimeDetector()
        ind = _indicators(adx=30.0)
        assert d.detect(ind) == RegimeType.TRENDING

    def test_adx_exactly_25_is_trending(self):
        d = MarketRegimeDetector()
        ind = _indicators(adx=25.0)
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
    def test_adx_below_20_is_ranging(self):
        d = MarketRegimeDetector()
        ind = _indicators(adx=15.0, bb_upper=2010.0, bb_lower=1990.0, bb_mid=2000.0)
        assert d.detect(ind) == RegimeType.RANGING

    def test_adx_just_below_20(self):
        d = MarketRegimeDetector()
        ind = _indicators(adx=19.9, bb_upper=2020.0, bb_lower=1980.0, bb_mid=2000.0)
        assert d.detect(ind) == RegimeType.RANGING

    def test_no_bb_data_with_low_adx_is_ranging(self):
        d = MarketRegimeDetector()
        ind = _indicators(adx=10.0)
        assert d.detect(ind) == RegimeType.RANGING


# ── BREAKOUT ─────────────────────────────────────────────────────────────────

class TestBreakoutRegime:
    def test_kc_bb_squeeze(self):
        """BB 完全在 KC 内 → BREAKOUT（最高优先级）。"""
        d = MarketRegimeDetector()
        # KC: 1990–2010, BB: 1995–2005 → squeeze
        ind = _indicators(
            adx=15.0,
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
        """ADX<20 且 BB 极窄 → BREAKOUT（蓄力盘整）。"""
        d = MarketRegimeDetector()
        # bb_width_pct = (2001 - 1999) / 2000 = 0.001 < 0.005
        ind = _indicators(adx=12.0, bb_upper=2001.0, bb_lower=1999.0, bb_mid=2000.0)
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
        # ADX<20 且 BB 宽度 = 30/2000 = 1.5% > 0.5% → RANGING
        assert d.detect(ind) == RegimeType.RANGING


# ── UNCERTAIN ────────────────────────────────────────────────────────────────

class TestUncertainRegime:
    def test_no_indicators_is_uncertain(self):
        d = MarketRegimeDetector()
        assert d.detect({}) == RegimeType.UNCERTAIN

    def test_adx_in_transition_zone(self):
        d = MarketRegimeDetector()
        ind = _indicators(adx=22.0)
        assert d.detect(ind) == RegimeType.UNCERTAIN

    def test_adx_exactly_20(self):
        d = MarketRegimeDetector()
        ind = _indicators(adx=20.0)
        assert d.detect(ind) == RegimeType.UNCERTAIN

    def test_adx_just_below_25(self):
        d = MarketRegimeDetector()
        ind = _indicators(adx=24.9)
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
        # bb_mid=0 → 无法计算宽度比例 → RANGING（adx<20）
        assert d.detect(ind) == RegimeType.RANGING

    def test_regime_type_is_string(self):
        """RegimeType 混入 str，可直接序列化。"""
        assert RegimeType.TRENDING == "trending"
        assert isinstance(RegimeType.TRENDING.value, str)
