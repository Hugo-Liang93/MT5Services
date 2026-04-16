"""tests/research/features/test_microstructure_provider.py

MicrostructureFeatureProvider 单元测试。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from src.research.features.protocol import FeatureRole
from src.research.features.microstructure import MicrostructureFeatureProvider
from src.research.core.config import MicrostructureProviderConfig


# ---------------------------------------------------------------------------
# Mock DataMatrix helpers
# ---------------------------------------------------------------------------


def _make_matrix(
    opens: List[float],
    highs: List[float],
    lows: List[float],
    closes: List[float],
    volumes: Optional[List[float]] = None,
    indicator_series: Optional[Dict[Tuple[str, str], List[Optional[float]]]] = None,
) -> Any:
    """构造最小化 DataMatrix mock。"""
    m = MagicMock()
    m.n_bars = len(closes)
    m.opens = opens
    m.highs = highs
    m.lows = lows
    m.closes = closes
    m.volumes = volumes if volumes is not None else [1000.0] * len(closes)
    m.indicator_series = indicator_series if indicator_series is not None else {}
    return m


# ---------------------------------------------------------------------------
# Provider 基本属性
# ---------------------------------------------------------------------------


class TestProviderMetadata:
    def test_name(self) -> None:
        p = MicrostructureFeatureProvider()
        assert p.name == "microstructure"

    def test_no_extra_data_required(self) -> None:
        p = MicrostructureFeatureProvider()
        assert p.required_extra_data() is None

    def test_feature_count_ge_21(self) -> None:
        p = MicrostructureFeatureProvider()
        assert p.feature_count >= 21

    def test_required_columns_contains_atr(self) -> None:
        p = MicrostructureFeatureProvider()
        cols = p.required_columns()
        assert ("atr14", "atr") in cols

    def test_required_columns_contains_bb_width(self) -> None:
        p = MicrostructureFeatureProvider()
        cols = p.required_columns()
        assert ("bb20", "bb_width") in cols


# ---------------------------------------------------------------------------
# Role mapping
# ---------------------------------------------------------------------------


class TestRoleMapping:
    def test_role_mapping_has_correct_entries(self) -> None:
        p = MicrostructureFeatureProvider()
        rm = p.role_mapping()
        # WHY 类
        assert rm["consecutive_up"] == FeatureRole.WHY
        assert rm["consecutive_down"] == FeatureRole.WHY
        assert rm["oc_imbalance"] == FeatureRole.WHY
        assert rm["consecutive_same_color"] == FeatureRole.WHY

        # WHEN 类
        assert rm["body_ratio"] == FeatureRole.WHEN
        assert rm["range_expansion"] == FeatureRole.WHEN
        assert rm["close_to_close_3"] == FeatureRole.WHEN
        assert rm["gap_ratio"] == FeatureRole.WHEN

        # WHERE 类
        assert rm["close_in_range"] == FeatureRole.WHERE
        assert rm["upper_wick_ratio"] == FeatureRole.WHERE
        assert rm["lower_wick_ratio"] == FeatureRole.WHERE

        # VOLUME 类
        assert rm["volume_surge"] == FeatureRole.VOLUME

    def test_windowed_feature_roles(self) -> None:
        p = MicrostructureFeatureProvider()
        rm = p.role_mapping()
        w = 5  # default lookback
        # WHY 窗口特征
        assert rm[f"hh_count_{w}"] == FeatureRole.WHY
        assert rm[f"ll_count_{w}"] == FeatureRole.WHY
        # WHEN 窗口特征
        assert rm[f"volatility_ratio_{w}"] == FeatureRole.WHEN
        assert rm[f"bb_width_change_{w}"] == FeatureRole.WHEN
        assert rm[f"avg_body_ratio_{w}"] == FeatureRole.WHEN
        # WHERE 窗口特征
        assert rm[f"upper_shadow_ratio_{w}"] == FeatureRole.WHERE
        assert rm[f"lower_shadow_ratio_{w}"] == FeatureRole.WHERE
        assert rm[f"range_position_{w}"] == FeatureRole.WHERE
        # VOLUME 窗口特征
        assert rm[f"vol_price_accord_{w}"] == FeatureRole.VOLUME


# ---------------------------------------------------------------------------
# close_in_range
# ---------------------------------------------------------------------------


class TestCloseInRange:
    def test_known_values(self) -> None:
        """close 在 range 中间 → 0.5"""
        m = _make_matrix(
            opens=[1.0],
            highs=[2.0],
            lows=[0.0],
            closes=[1.0],  # (1-0)/(2-0) = 0.5
        )
        p = MicrostructureFeatureProvider()
        result = p.compute(m)
        col = result[("microstructure", "close_in_range")]
        assert col[0] == pytest.approx(0.5)

    def test_close_at_high(self) -> None:
        m = _make_matrix(
            opens=[1.0],
            highs=[3.0],
            lows=[1.0],
            closes=[3.0],  # (3-1)/(3-1) = 1.0
        )
        p = MicrostructureFeatureProvider()
        result = p.compute(m)
        col = result[("microstructure", "close_in_range")]
        assert col[0] == pytest.approx(1.0)

    def test_zero_range_returns_half(self) -> None:
        """high=low → range < 1e-9 → 返回 0.5"""
        m = _make_matrix(
            opens=[1.0],
            highs=[1.0],
            lows=[1.0],
            closes=[1.0],
        )
        p = MicrostructureFeatureProvider()
        result = p.compute(m)
        col = result[("microstructure", "close_in_range")]
        assert col[0] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# body_ratio
# ---------------------------------------------------------------------------


class TestBodyRatio:
    def test_full_body_bar(self) -> None:
        """open=low, close=high → body_ratio=1.0"""
        m = _make_matrix(
            opens=[1.0],
            highs=[3.0],
            lows=[1.0],
            closes=[3.0],
        )
        p = MicrostructureFeatureProvider()
        result = p.compute(m)
        col = result[("microstructure", "body_ratio")]
        assert col[0] == pytest.approx(1.0)

    def test_doji_bar(self) -> None:
        """open=close → body_ratio=0.0"""
        m = _make_matrix(
            opens=[2.0],
            highs=[3.0],
            lows=[1.0],
            closes=[2.0],
        )
        p = MicrostructureFeatureProvider()
        result = p.compute(m)
        col = result[("microstructure", "body_ratio")]
        assert col[0] == pytest.approx(0.0)

    def test_half_body(self) -> None:
        """|close-open|=1, range=2 → 0.5"""
        m = _make_matrix(
            opens=[1.0],
            highs=[3.0],
            lows=[1.0],
            closes=[2.0],
        )
        p = MicrostructureFeatureProvider()
        result = p.compute(m)
        col = result[("microstructure", "body_ratio")]
        assert col[0] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# consecutive_up / consecutive_down
# ---------------------------------------------------------------------------


class TestConsecutiveUpDown:
    def test_consecutive_up_known_sequence(self) -> None:
        """closes: 1, 2, 3, 4, 5 → 连续上涨 0,1,2,3,4"""
        closes = [1.0, 2.0, 3.0, 4.0, 5.0]
        m = _make_matrix(
            opens=closes,
            highs=closes,
            lows=closes,
            closes=closes,
        )
        p = MicrostructureFeatureProvider()
        result = p.compute(m)
        col = result[("microstructure", "consecutive_up")]
        # 第一个 bar 无前值 → 0
        assert col[0] == pytest.approx(0.0)
        assert col[1] == pytest.approx(1.0)
        assert col[2] == pytest.approx(2.0)
        assert col[3] == pytest.approx(3.0)
        assert col[4] == pytest.approx(4.0)

    def test_consecutive_down_known_sequence(self) -> None:
        """closes: 5, 4, 3, 2, 1 → 连续下跌 0,1,2,3,4"""
        closes = [5.0, 4.0, 3.0, 2.0, 1.0]
        m = _make_matrix(
            opens=closes,
            highs=closes,
            lows=closes,
            closes=closes,
        )
        p = MicrostructureFeatureProvider()
        result = p.compute(m)
        col = result[("microstructure", "consecutive_down")]
        assert col[0] == pytest.approx(0.0)
        assert col[1] == pytest.approx(1.0)
        assert col[2] == pytest.approx(2.0)

    def test_consecutive_up_resets_on_down(self) -> None:
        """上涨后出现下跌 → 计数重置"""
        closes = [1.0, 2.0, 3.0, 2.5, 3.5]
        m = _make_matrix(
            opens=closes,
            highs=closes,
            lows=closes,
            closes=closes,
        )
        p = MicrostructureFeatureProvider()
        result = p.compute(m)
        col = result[("microstructure", "consecutive_up")]
        # bar3: close=2.5 < 3.0 → 不是上涨 → 0
        assert col[3] == pytest.approx(0.0)
        # bar4: close=3.5 > 2.5 → 1
        assert col[4] == pytest.approx(1.0)

    def test_consecutive_down_resets_on_up(self) -> None:
        """下跌后出现上涨 → 计数重置"""
        closes = [5.0, 4.0, 3.0, 3.5, 2.5]
        m = _make_matrix(
            opens=closes,
            highs=closes,
            lows=closes,
            closes=closes,
        )
        p = MicrostructureFeatureProvider()
        result = p.compute(m)
        col = result[("microstructure", "consecutive_down")]
        # bar3: close=3.5 > 3.0 → 不是下跌 → 0
        assert col[3] == pytest.approx(0.0)
        # bar4: close=2.5 < 3.5 → 1
        assert col[4] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# volume_surge
# ---------------------------------------------------------------------------


class TestVolumeSurge:
    def test_known_volume_data(self) -> None:
        """前 w 个 bar volume=1000，第 w+1 bar volume=5000 → surge=5.0"""
        w = 5
        volumes = [1000.0] * w + [5000.0]
        closes = [1.0] * (w + 1)
        m = _make_matrix(
            opens=closes,
            highs=closes,
            lows=closes,
            closes=closes,
            volumes=volumes,
        )
        p = MicrostructureFeatureProvider()
        result = p.compute(m)
        col = result[("microstructure", "volume_surge")]
        # bar w (index=5): volume=5000 / mean([1000]*5) = 5.0
        assert col[w] == pytest.approx(5.0)

    def test_volume_surge_early_bars_none(self) -> None:
        """窗口不足时返回 None"""
        w = 5
        volumes = [1000.0] * 3
        closes = [1.0] * 3
        m = _make_matrix(
            opens=closes,
            highs=closes,
            lows=closes,
            closes=closes,
            volumes=volumes,
        )
        p = MicrostructureFeatureProvider()
        result = p.compute(m)
        col = result[("microstructure", "volume_surge")]
        # n=3 < w=5，全部 None
        for v in col:
            assert v is None


# ---------------------------------------------------------------------------
# range_position_{w}
# ---------------------------------------------------------------------------


class TestRangePosition:
    def test_close_at_min_low(self) -> None:
        """过去 w 个 bar 中，close=min(low) → range_position=0.0"""
        w = 5
        # lows: [0,1,2,3,4], highs: [5,5,5,5,5]
        # closes=[5] * w (all at max) 然后第 w+1 bar close=0 (at min)
        lows = [0.0, 1.0, 2.0, 3.0, 4.0, 0.0]
        highs = [5.0, 5.0, 5.0, 5.0, 5.0, 5.0]
        closes = [3.0, 3.0, 3.0, 3.0, 3.0, 0.0]
        opens = [3.0] * 6
        m = _make_matrix(opens=opens, highs=highs, lows=lows, closes=closes)
        p = MicrostructureFeatureProvider()
        result = p.compute(m)
        col = result[("microstructure", f"range_position_{w}")]
        # bar 5: close=0, min(lows[1:6])=0, max(highs[1:6])=5 → (0-0)/(5-0)=0.0
        assert col[w] == pytest.approx(0.0)

    def test_close_at_max_high(self) -> None:
        """close=max(high) → range_position=1.0"""
        w = 5
        lows = [0.0] * 6
        highs = [5.0, 4.0, 3.0, 2.0, 1.0, 5.0]
        closes = [2.0, 2.0, 2.0, 2.0, 2.0, 5.0]
        opens = [2.0] * 6
        m = _make_matrix(opens=opens, highs=highs, lows=lows, closes=closes)
        p = MicrostructureFeatureProvider()
        result = p.compute(m)
        col = result[("microstructure", f"range_position_{w}")]
        # bar 5: close=5, max(highs[1:6])=5, min(lows[1:6])=0 → (5-0)/(5-0)=1.0
        assert col[w] == pytest.approx(1.0)

    def test_range_position_early_none(self) -> None:
        """窗口不足时返回 None"""
        w = 5
        closes = [1.0] * 3
        m = _make_matrix(
            opens=closes, highs=closes, lows=closes, closes=closes
        )
        p = MicrostructureFeatureProvider()
        result = p.compute(m)
        col = result[("microstructure", f"range_position_{w}")]
        for v in col:
            assert v is None


# ---------------------------------------------------------------------------
# range_expansion (needs ATR)
# ---------------------------------------------------------------------------


class TestRangeExpansion:
    def test_none_when_atr_missing(self) -> None:
        """无 ATR indicator_series → 全部 None"""
        m = _make_matrix(
            opens=[1.0, 2.0],
            highs=[2.0, 3.0],
            lows=[0.0, 1.0],
            closes=[1.5, 2.5],
        )
        p = MicrostructureFeatureProvider()
        result = p.compute(m)
        col = result[("microstructure", "range_expansion")]
        assert all(v is None for v in col)

    def test_known_atr_value(self) -> None:
        """range=2, atr=2 → range_expansion=1.0"""
        m = _make_matrix(
            opens=[1.0],
            highs=[3.0],
            lows=[1.0],
            closes=[2.0],
            indicator_series={("atr14", "atr"): [2.0]},
        )
        p = MicrostructureFeatureProvider()
        result = p.compute(m)
        col = result[("microstructure", "range_expansion")]
        assert col[0] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------


class TestOutputShape:
    def test_all_columns_correct_length(self) -> None:
        n = 10
        closes = [float(i) for i in range(1, n + 1)]
        opens = [float(i) - 0.1 for i in range(1, n + 1)]
        highs = [float(i) + 0.5 for i in range(1, n + 1)]
        lows = [float(i) - 0.5 for i in range(1, n + 1)]
        volumes = [1000.0 + i * 10 for i in range(n)]
        m = _make_matrix(opens=opens, highs=highs, lows=lows, closes=closes, volumes=volumes)
        p = MicrostructureFeatureProvider()
        result = p.compute(m)
        for key, col in result.items():
            assert len(col) == n, f"{key} 长度 {len(col)} ≠ {n}"

    def test_all_keys_have_microstructure_group(self) -> None:
        n = 5
        closes = [1.0, 2.0, 3.0, 2.0, 1.0]
        m = _make_matrix(
            opens=closes, highs=closes, lows=closes, closes=closes
        )
        p = MicrostructureFeatureProvider()
        result = p.compute(m)
        for group, _field in result:
            assert group == "microstructure", f"意外的 group: {group!r}"

    def test_empty_matrix(self) -> None:
        m = MagicMock()
        m.n_bars = 0
        m.opens = []
        m.highs = []
        m.lows = []
        m.closes = []
        m.volumes = []
        m.indicator_series = {}
        p = MicrostructureFeatureProvider()
        result = p.compute(m)
        for col in result.values():
            assert col == []

    def test_feature_count_matches_output(self) -> None:
        """feature_count 属性应与实际 compute() 输出列数一致。"""
        n = 10
        closes = [float(i) for i in range(1, n + 1)]
        opens = [c - 0.1 for c in closes]
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        m = _make_matrix(opens=opens, highs=highs, lows=lows, closes=closes)
        p = MicrostructureFeatureProvider()
        result = p.compute(m)
        assert len(result) == p.feature_count

    def test_config_lookback_respected(self) -> None:
        """自定义 lookback=3 → 窗口特征名包含 _3 后缀。"""
        p = MicrostructureFeatureProvider(config=MicrostructureProviderConfig(lookback=3))
        rm = p.role_mapping()
        assert any("_3" in k for k in rm)
