"""tests/research/features/test_intrabar_provider.py

IntrabarFeatureProvider 单元测试。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from src.research.features.protocol import FeatureRole
from src.research.features.intrabar import IntrabarProvider


# ---------------------------------------------------------------------------
# Mock helper
# ---------------------------------------------------------------------------


def _make_child(
    open_: float,
    close: float,
    high: float,
    low: float,
    volume: float = 100.0,
) -> Any:
    """构造子 bar mock 对象。"""
    b = MagicMock()
    b.open = open_
    b.close = close
    b.high = high
    b.low = low
    b.volume = volume
    return b


def _make_matrix(
    opens: List[float],
    closes: List[float],
    child_bars: Optional[Dict[int, List[Any]]] = None,
    child_tf: str = "M15",
    timeframe: str = "H1",
) -> Any:
    """构造最小化 DataMatrix mock（仅 intrabar 所需字段）。"""
    m = MagicMock()
    m.n_bars = len(closes)
    m.opens = opens
    m.closes = closes
    m.child_bars = child_bars if child_bars is not None else {}
    m.child_tf = child_tf
    m.timeframe = timeframe
    return m


# ---------------------------------------------------------------------------
# Provider 基本属性
# ---------------------------------------------------------------------------


class TestProviderMetadata:
    def test_name(self) -> None:
        p = IntrabarProvider()
        assert p.name == "intrabar"

    def test_feature_count(self) -> None:
        p = IntrabarProvider()
        assert p.feature_count == 5

    def test_required_columns_empty(self) -> None:
        p = IntrabarProvider()
        assert p.required_columns() == []

    def test_no_extra_data_required(self) -> None:
        p = IntrabarProvider()
        assert p.required_extra_data() is None

    def test_role_mapping(self) -> None:
        p = IntrabarProvider()
        rm = p.role_mapping()
        assert rm["child_bar_consensus"] == FeatureRole.WHY
        assert rm["child_range_acceleration"] == FeatureRole.WHEN
        assert rm["intrabar_momentum_shift"] == FeatureRole.WHY
        assert rm["child_volume_front_weight"] == FeatureRole.WHEN
        assert rm["child_bar_count_ratio"] == FeatureRole.WHERE


# ---------------------------------------------------------------------------
# 空 child_bars → 返回空 dict
# ---------------------------------------------------------------------------


class TestEmptyChildBars:
    def test_empty_child_bars_returns_empty_dict(self) -> None:
        """child_bars={} 时 compute() 返回空 dict。"""
        m = _make_matrix([1.0, 2.0], [1.1, 2.1], child_bars={})
        p = IntrabarProvider()
        result = p.compute(m)
        assert result == {}

    def test_none_result_on_empty_child_bars(self) -> None:
        """确认空 child_bars 不会产生任何列。"""
        m = _make_matrix([1.0], [1.1], child_bars={})
        p = IntrabarProvider()
        result = p.compute(m)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# child_bar_consensus
# ---------------------------------------------------------------------------


class TestChildBarConsensus:
    def test_all_children_same_color_as_parent(self) -> None:
        """父 bar 阳线，子 bars 全阳 → consensus = 1.0。"""
        children = [
            _make_child(1.0, 1.1, 1.2, 0.9),
            _make_child(1.1, 1.2, 1.3, 1.0),
            _make_child(1.2, 1.3, 1.4, 1.1),
        ]
        m = _make_matrix(
            opens=[1.0],
            closes=[1.3],  # 父 bar 阳线
            child_bars={0: children},
        )
        p = IntrabarProvider()
        result = p.compute(m)
        vals = result[("intrabar", "child_bar_consensus")]
        assert vals[0] == pytest.approx(1.0)

    def test_half_children_same_color(self) -> None:
        """父 bar 阳线，子 bars 2 阳 2 阴 → consensus = 0.5。"""
        children = [
            _make_child(1.0, 1.1, 1.2, 0.9),   # 阳
            _make_child(1.1, 1.0, 1.2, 0.9),   # 阴
            _make_child(1.0, 1.1, 1.2, 0.9),   # 阳
            _make_child(1.1, 1.0, 1.2, 0.9),   # 阴
        ]
        m = _make_matrix(
            opens=[1.0],
            closes=[1.1],  # 父 bar 阳线
            child_bars={0: children},
        )
        p = IntrabarProvider()
        result = p.compute(m)
        vals = result[("intrabar", "child_bar_consensus")]
        assert vals[0] == pytest.approx(0.5)

    def test_too_few_children_returns_none(self) -> None:
        """子 bar 数 < 2 → None。"""
        children = [_make_child(1.0, 1.1, 1.2, 0.9)]
        m = _make_matrix(
            opens=[1.0],
            closes=[1.1],
            child_bars={0: children},
        )
        p = IntrabarProvider()
        result = p.compute(m)
        vals = result[("intrabar", "child_bar_consensus")]
        assert vals[0] is None

    def test_no_children_for_bar_returns_none(self) -> None:
        """该 bar 无子 bars（child_bars 不含该 idx）→ None。"""
        # 有 2 个 bar，但 child_bars 只提供了 bar 0 的子 bars
        children = [
            _make_child(1.0, 1.1, 1.2, 0.9),
            _make_child(1.1, 1.2, 1.3, 1.0),
        ]
        m = _make_matrix(
            opens=[1.0, 2.0],
            closes=[1.3, 2.3],
            child_bars={0: children},  # bar 1 没有子 bars
        )
        p = IntrabarProvider()
        result = p.compute(m)
        vals = result[("intrabar", "child_bar_consensus")]
        assert vals[0] == pytest.approx(1.0)
        assert vals[1] is None


# ---------------------------------------------------------------------------
# child_range_acceleration
# ---------------------------------------------------------------------------


class TestChildRangeAcceleration:
    def test_range_acceleration_doubling(self) -> None:
        """后半段 range 是前半段两倍 → acceleration = 1.0。"""
        # 4 个子 bars：前 2 range=1, 后 2 range=2
        children = [
            _make_child(1.0, 1.0, 2.0, 1.0),   # range=1
            _make_child(1.0, 1.0, 2.0, 1.0),   # range=1
            _make_child(1.0, 1.0, 3.0, 1.0),   # range=2
            _make_child(1.0, 1.0, 3.0, 1.0),   # range=2
        ]
        m = _make_matrix(opens=[1.0], closes=[1.0], child_bars={0: children})
        p = IntrabarProvider()
        result = p.compute(m)
        vals = result[("intrabar", "child_range_acceleration")]
        assert vals[0] == pytest.approx(1.0)

    def test_too_few_children_returns_none(self) -> None:
        """子 bar 数 < 4 → None。"""
        children = [
            _make_child(1.0, 1.1, 1.2, 0.9),
            _make_child(1.0, 1.1, 1.2, 0.9),
        ]
        m = _make_matrix(opens=[1.0], closes=[1.1], child_bars={0: children})
        p = IntrabarProvider()
        result = p.compute(m)
        vals = result[("intrabar", "child_range_acceleration")]
        assert vals[0] is None


# ---------------------------------------------------------------------------
# intrabar_momentum_shift
# ---------------------------------------------------------------------------


class TestIntrabarMomentumShift:
    def test_alternating_direction_max_shift(self) -> None:
        """close 交替涨跌 → shift 接近 1.0。"""
        # close: 1.0, 1.1, 1.0, 1.1, 1.0 → 方向切换 3 次，共 4 间隔 → shift=0.75
        children = [
            _make_child(1.0, 1.0, 1.1, 0.9),
            _make_child(1.0, 1.1, 1.2, 1.0),
            _make_child(1.0, 1.0, 1.1, 0.9),
            _make_child(1.0, 1.1, 1.2, 1.0),
            _make_child(1.0, 1.0, 1.1, 0.9),
        ]
        m = _make_matrix(opens=[1.0], closes=[1.0], child_bars={0: children})
        p = IntrabarProvider()
        result = p.compute(m)
        vals = result[("intrabar", "intrabar_momentum_shift")]
        # 3 changes / 4 intervals = 0.75
        assert vals[0] == pytest.approx(0.75)

    def test_no_direction_change(self) -> None:
        """close 单调上升 → shift = 0.0。"""
        children = [
            _make_child(1.0, 1.0, 1.1, 0.9),
            _make_child(1.0, 1.1, 1.2, 1.0),
            _make_child(1.0, 1.2, 1.3, 1.1),
            _make_child(1.0, 1.3, 1.4, 1.2),
        ]
        m = _make_matrix(opens=[1.0], closes=[1.3], child_bars={0: children})
        p = IntrabarProvider()
        result = p.compute(m)
        vals = result[("intrabar", "intrabar_momentum_shift")]
        assert vals[0] == pytest.approx(0.0)

    def test_too_few_children_returns_none(self) -> None:
        """子 bar 数 < 3 → None。"""
        children = [
            _make_child(1.0, 1.1, 1.2, 0.9),
            _make_child(1.1, 1.2, 1.3, 1.0),
        ]
        m = _make_matrix(opens=[1.0], closes=[1.2], child_bars={0: children})
        p = IntrabarProvider()
        result = p.compute(m)
        vals = result[("intrabar", "intrabar_momentum_shift")]
        assert vals[0] is None


# ---------------------------------------------------------------------------
# child_volume_front_weight
# ---------------------------------------------------------------------------


class TestChildVolumeFrontWeight:
    def test_equal_volume_front_weight(self) -> None:
        """前后 volume 相等 → front_weight = 0.5。"""
        children = [
            _make_child(1.0, 1.1, 1.2, 0.9, volume=100.0),
            _make_child(1.1, 1.2, 1.3, 1.0, volume=100.0),
            _make_child(1.2, 1.3, 1.4, 1.1, volume=100.0),
            _make_child(1.3, 1.4, 1.5, 1.2, volume=100.0),
        ]
        m = _make_matrix(opens=[1.0], closes=[1.4], child_bars={0: children})
        p = IntrabarProvider()
        result = p.compute(m)
        vals = result[("intrabar", "child_volume_front_weight")]
        assert vals[0] == pytest.approx(0.5)

    def test_front_heavy_volume(self) -> None:
        """前半 volume=200, 后半 volume=100 → front_weight = 2/3。"""
        children = [
            _make_child(1.0, 1.1, 1.2, 0.9, volume=200.0),
            _make_child(1.1, 1.2, 1.3, 1.0, volume=200.0),
            _make_child(1.2, 1.3, 1.4, 1.1, volume=100.0),
            _make_child(1.3, 1.4, 1.5, 1.2, volume=100.0),
        ]
        m = _make_matrix(opens=[1.0], closes=[1.4], child_bars={0: children})
        p = IntrabarProvider()
        result = p.compute(m)
        vals = result[("intrabar", "child_volume_front_weight")]
        assert vals[0] == pytest.approx(400.0 / 600.0)

    def test_too_few_children_returns_none(self) -> None:
        """子 bar 数 < 4 → None。"""
        children = [
            _make_child(1.0, 1.1, 1.2, 0.9, volume=100.0),
        ]
        m = _make_matrix(opens=[1.0], closes=[1.1], child_bars={0: children})
        p = IntrabarProvider()
        result = p.compute(m)
        vals = result[("intrabar", "child_volume_front_weight")]
        assert vals[0] is None


# ---------------------------------------------------------------------------
# child_bar_count_ratio
# ---------------------------------------------------------------------------


class TestChildBarCountRatio:
    def test_full_count_ratio(self) -> None:
        """H1/M15 预期 4 个子 bars，实际 4 个 → ratio = 1.0。"""
        children = [
            _make_child(1.0, 1.1, 1.2, 0.9) for _ in range(4)
        ]
        m = _make_matrix(
            opens=[1.0],
            closes=[1.1],
            child_bars={0: children},
            child_tf="M15",
            timeframe="H1",
        )
        p = IntrabarProvider()
        result = p.compute(m)
        vals = result[("intrabar", "child_bar_count_ratio")]
        assert vals[0] == pytest.approx(1.0)

    def test_half_count_ratio(self) -> None:
        """H1/M15 预期 4 个子 bars，实际 2 个 → ratio = 0.5。"""
        children = [
            _make_child(1.0, 1.1, 1.2, 0.9) for _ in range(2)
        ]
        m = _make_matrix(
            opens=[1.0],
            closes=[1.1],
            child_bars={0: children},
            child_tf="M15",
            timeframe="H1",
        )
        p = IntrabarProvider()
        result = p.compute(m)
        vals = result[("intrabar", "child_bar_count_ratio")]
        assert vals[0] == pytest.approx(0.5)

    def test_no_child_tf_returns_none(self) -> None:
        """child_tf 为空 → None。"""
        children = [_make_child(1.0, 1.1, 1.2, 0.9) for _ in range(4)]
        m = _make_matrix(
            opens=[1.0],
            closes=[1.1],
            child_bars={0: children},
            child_tf="",
            timeframe="H1",
        )
        p = IntrabarProvider()
        result = p.compute(m)
        vals = result[("intrabar", "child_bar_count_ratio")]
        assert vals[0] is None


# ---------------------------------------------------------------------------
# compute() 返回结构
# ---------------------------------------------------------------------------


class TestComputeOutputStructure:
    def test_output_keys_present(self) -> None:
        """有子 bars 时返回所有 5 个预期的键。"""
        children = [_make_child(1.0, 1.1, 1.2, 0.9) for _ in range(4)]
        m = _make_matrix(
            opens=[1.0],
            closes=[1.1],
            child_bars={0: children},
        )
        p = IntrabarProvider()
        result = p.compute(m)
        expected_fields = [
            "child_bar_consensus",
            "child_range_acceleration",
            "intrabar_momentum_shift",
            "child_volume_front_weight",
            "child_bar_count_ratio",
        ]
        for field in expected_fields:
            assert ("intrabar", field) in result, f"缺少键: {field}"

    def test_output_length_matches_n_bars(self) -> None:
        """每列长度等于 n_bars。"""
        children = [_make_child(1.0, 1.1, 1.2, 0.9) for _ in range(4)]
        n = 3
        m = _make_matrix(
            opens=[1.0] * n,
            closes=[1.1] * n,
            child_bars={0: children},  # 只有 bar 0 有子 bars
        )
        p = IntrabarProvider()
        result = p.compute(m)
        for key, vals in result.items():
            assert len(vals) == n, f"{key} 长度不匹配"
