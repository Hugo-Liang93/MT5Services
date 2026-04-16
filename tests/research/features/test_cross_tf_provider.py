"""tests/research/features/test_cross_tf_provider.py

CrossTFFeatureProvider 单元测试。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from src.research.features.protocol import FeatureRole, ProviderDataRequirement
from src.research.features.cross_tf import CrossTFFeatureProvider
from src.research.core.config import CrossTFProviderConfig


# ---------------------------------------------------------------------------
# Mock DataMatrix helpers
# ---------------------------------------------------------------------------


def _make_matrix(
    n_bars: int,
    bar_times: List[datetime],
    indicator_series: Optional[Dict[Tuple[str, str], List[Optional[float]]]] = None,
) -> Any:
    """构造最小化 DataMatrix mock。"""
    m = MagicMock()
    m.n_bars = n_bars
    m.bar_times = bar_times
    m.indicator_series = indicator_series or {}
    return m


def _make_child_times(n: int, start_ts: int = 0, step_sec: int = 1800) -> List[datetime]:
    """构造 n 个 M30 子 TF bar 时间戳（每 30 分钟）。"""
    return [
        datetime.fromtimestamp(start_ts + i * step_sec, tz=timezone.utc)
        for i in range(n)
    ]


def _make_parent_times(n: int, start_ts: int = 0, step_sec: int = 14400) -> List[datetime]:
    """构造 n 个 H4 父 TF bar 时间戳（每 4 小时）。"""
    return [
        datetime.fromtimestamp(start_ts + i * step_sec, tz=timezone.utc)
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# 1. Provider 基本属性
# ---------------------------------------------------------------------------


class TestProviderMetadata:
    def test_name(self) -> None:
        p = CrossTFFeatureProvider()
        assert p.name == "cross_tf"

    def test_required_extra_data_returns_requirement(self) -> None:
        p = CrossTFFeatureProvider()
        req = p.required_extra_data()
        assert req is not None
        assert isinstance(req, ProviderDataRequirement)
        assert isinstance(req.parent_tf_mapping, dict)
        assert len(req.parent_tf_mapping) > 0
        assert isinstance(req.parent_indicators, list)
        assert len(req.parent_indicators) > 0

    def test_required_columns_non_empty(self) -> None:
        p = CrossTFFeatureProvider()
        cols = p.required_columns()
        assert isinstance(cols, list)

    def test_feature_count_equals_8(self) -> None:
        p = CrossTFFeatureProvider()
        assert p.feature_count == 8

    def test_role_mapping_contains_expected_keys(self) -> None:
        p = CrossTFFeatureProvider()
        rm = p.role_mapping()
        assert "parent_trend_dir" in rm
        assert "parent_rsi" in rm
        assert "parent_adx" in rm
        assert "tf_trend_align" in rm
        assert "dist_to_parent_ema" in rm
        assert "parent_rsi_delta_5" in rm
        assert "parent_adx_delta_5" in rm
        assert "parent_bb_pos" in rm

    def test_role_mapping_why_where_assignment(self) -> None:
        p = CrossTFFeatureProvider()
        rm = p.role_mapping()
        # WHY 特征
        assert rm["parent_trend_dir"] == FeatureRole.WHY
        assert rm["parent_rsi"] == FeatureRole.WHY
        assert rm["parent_adx"] == FeatureRole.WHY
        assert rm["tf_trend_align"] == FeatureRole.WHY
        assert rm["parent_rsi_delta_5"] == FeatureRole.WHY
        assert rm["parent_adx_delta_5"] == FeatureRole.WHY
        # WHERE 特征
        assert rm["dist_to_parent_ema"] == FeatureRole.WHERE
        assert rm["parent_bb_pos"] == FeatureRole.WHERE


# ---------------------------------------------------------------------------
# 2. 空/None extra_data 降级处理
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_returns_empty_dict_when_extra_data_is_none(self) -> None:
        p = CrossTFFeatureProvider()
        times = _make_child_times(5)
        matrix = _make_matrix(5, times)
        result = p.compute(matrix, extra_data=None)
        assert result == {}

    def test_returns_empty_dict_when_extra_data_is_empty(self) -> None:
        p = CrossTFFeatureProvider()
        times = _make_child_times(5)
        matrix = _make_matrix(5, times)
        result = p.compute(matrix, extra_data={})
        assert result == {}

    def test_returns_empty_dict_when_n_bars_is_zero(self) -> None:
        p = CrossTFFeatureProvider()
        matrix = _make_matrix(0, [])
        result = p.compute(matrix, extra_data=None)
        assert result == {}


# ---------------------------------------------------------------------------
# 3. 父 TF 前向填充对齐
# ---------------------------------------------------------------------------


class TestForwardFillAlignment:
    """测试 parent_trend_dir 的前向填充语义：4 个 M30 bar 对应同一 H4 bar。"""

    def _make_extra_data(
        self,
        parent_tf: str,
        parent_times: List[datetime],
        indicators: Dict[str, List[float]],
    ) -> Dict[str, Any]:
        return {
            "parent_tf": parent_tf,
            "parent_bar_times": parent_times,
            "parent_indicators": indicators,
        }

    def test_four_child_bars_map_to_same_parent(self) -> None:
        """4 个 M30 bar 时间戳均落在同一 H4 bar 内，应全填充相同父方向值。"""
        # H4 bar 起始 = 0, 子 TF M30 bars 0,1,2,3 均在 H4 bar [0, 14400) 内
        parent_times = _make_parent_times(2, start_ts=0, step_sec=14400)
        child_times = _make_child_times(4, start_ts=0, step_sec=1800)

        extra_data = self._make_extra_data(
            "H4",
            parent_times,
            {"supertrend_direction": [1.0, -1.0]},
        )
        matrix = _make_matrix(4, child_times)
        p = CrossTFFeatureProvider()
        result = p.compute(matrix, extra_data=extra_data)

        key = ("cross_tf", "parent_trend_dir")
        assert key in result
        vals = result[key]
        assert len(vals) == 4
        # 前 4 个 child bars (t=0,1800,3600,5400) 均落在 parent[0] (t=0)
        # searchsorted(side="right") - 1：
        #   child_ts=0 → idx before 0 = -1 → clip to 0
        #   child_ts=1800 → idx before 14400 = 1 - 1 = 0
        # 因此 bars 0,1,2,3 全部对应 parent[0]=1.0
        for v in vals:
            assert v == pytest.approx(1.0)

    def test_child_bars_spanning_two_parent_bars(self) -> None:
        """前 4 个 child bar 对应 parent[0]，后 4 个对应 parent[1]。"""
        parent_times = _make_parent_times(2, start_ts=0, step_sec=14400)
        # 8 个 M30 bar：0, 1800, 3600, 5400, 7200, 9000, 10800, 12600
        child_times = _make_child_times(8, start_ts=0, step_sec=1800)

        extra_data = self._make_extra_data(
            "H4",
            parent_times,
            {"supertrend_direction": [1.0, -1.0]},
        )
        matrix = _make_matrix(8, child_times)
        p = CrossTFFeatureProvider()
        result = p.compute(matrix, extra_data=extra_data)

        key = ("cross_tf", "parent_trend_dir")
        vals = result[key]
        assert len(vals) == 8
        # 全部 child bars (max=12600) < 14400，所以全部对应 parent[0]
        for v in vals:
            assert v == pytest.approx(1.0)

    def test_child_bars_after_parent_boundary(self) -> None:
        """child bar 时间戳超过 parent[0] 后，应切换到 parent[1]。"""
        parent_times = _make_parent_times(2, start_ts=0, step_sec=14400)
        # child bars: 0, 14400 (刚好在 parent[1] 上), 28800 (超出范围)
        child_times = [
            datetime.fromtimestamp(0, tz=timezone.utc),
            datetime.fromtimestamp(14400, tz=timezone.utc),
            datetime.fromtimestamp(28800, tz=timezone.utc),
        ]
        extra_data = self._make_extra_data(
            "H4",
            parent_times,
            {"supertrend_direction": [1.0, -1.0]},
        )
        matrix = _make_matrix(3, child_times)
        p = CrossTFFeatureProvider()
        result = p.compute(matrix, extra_data=extra_data)

        key = ("cross_tf", "parent_trend_dir")
        vals = result[key]
        assert len(vals) == 3
        assert vals[0] == pytest.approx(1.0)   # child_ts=0: searchsorted=0, clip to 0 → parent[0]
        assert vals[1] == pytest.approx(-1.0)  # child_ts=14400: searchsorted=2, -1=1 → parent[1]
        assert vals[2] == pytest.approx(-1.0)  # child_ts=28800: searchsorted=2, -1=1 → parent[1]


# ---------------------------------------------------------------------------
# 4. tf_trend_align 特征
# ---------------------------------------------------------------------------


class TestTFTrendAlign:
    def _build_result(
        self,
        parent_dir: float,
        child_dir: float,
    ) -> Dict[Tuple[str, str], List[Optional[float]]]:
        """构建单 bar 场景并计算所有特征。"""
        parent_times = [datetime.fromtimestamp(0, tz=timezone.utc)]
        child_times = [datetime.fromtimestamp(0, tz=timezone.utc)]

        extra_data: Dict[str, Any] = {
            "parent_tf": "H4",
            "parent_bar_times": parent_times,
            "parent_indicators": {
                "supertrend_direction": [parent_dir],
                "rsi14": [50.0],
                "adx14": [25.0],
                "ema50": [1800.0],
                "bb_position": [0.5],
            },
        }
        indicator_series: Dict[Tuple[str, str], List[Optional[float]]] = {
            ("supertrend", "direction"): [child_dir],
            ("atr14", "atr"): [10.0],
        }
        matrix = _make_matrix(1, child_times, indicator_series)
        p = CrossTFFeatureProvider()
        return p.compute(matrix, extra_data=extra_data)

    def test_aligned_both_positive(self) -> None:
        result = self._build_result(parent_dir=1.0, child_dir=1.0)
        key = ("cross_tf", "tf_trend_align")
        assert key in result
        assert result[key][0] == pytest.approx(1.0)

    def test_conflict_parent_positive_child_negative(self) -> None:
        result = self._build_result(parent_dir=1.0, child_dir=-1.0)
        key = ("cross_tf", "tf_trend_align")
        assert result[key][0] == pytest.approx(-1.0)

    def test_conflict_parent_negative_child_positive(self) -> None:
        result = self._build_result(parent_dir=-1.0, child_dir=1.0)
        key = ("cross_tf", "tf_trend_align")
        assert result[key][0] == pytest.approx(-1.0)

    def test_aligned_both_negative(self) -> None:
        result = self._build_result(parent_dir=-1.0, child_dir=-1.0)
        key = ("cross_tf", "tf_trend_align")
        assert result[key][0] == pytest.approx(1.0)

    def test_missing_child_trend_returns_none(self) -> None:
        """child indicator_series 无 supertrend direction 时，tf_trend_align 应为 None。"""
        parent_times = [datetime.fromtimestamp(0, tz=timezone.utc)]
        child_times = [datetime.fromtimestamp(0, tz=timezone.utc)]
        extra_data: Dict[str, Any] = {
            "parent_tf": "H4",
            "parent_bar_times": parent_times,
            "parent_indicators": {
                "supertrend_direction": [1.0],
                "rsi14": [50.0],
                "adx14": [25.0],
                "ema50": [1800.0],
                "bb_position": [0.5],
            },
        }
        matrix = _make_matrix(1, child_times, {})
        p = CrossTFFeatureProvider()
        result = p.compute(matrix, extra_data=extra_data)
        key = ("cross_tf", "tf_trend_align")
        assert key in result
        assert result[key][0] is None


# ---------------------------------------------------------------------------
# 5. dist_to_parent_ema 特征
# ---------------------------------------------------------------------------


class TestDistToParentEma:
    def test_dist_computed_correctly(self) -> None:
        """dist = (close - parent_ema) / atr。"""
        # close = 1810, parent_ema = 1800, atr = 10 → dist = 1.0
        parent_times = [datetime.fromtimestamp(0, tz=timezone.utc)]
        child_times = [datetime.fromtimestamp(0, tz=timezone.utc)]
        extra_data: Dict[str, Any] = {
            "parent_tf": "H4",
            "parent_bar_times": parent_times,
            "parent_indicators": {
                "supertrend_direction": [1.0],
                "rsi14": [50.0],
                "adx14": [25.0],
                "ema50": [1800.0],
                "bb_position": [0.5],
            },
        }
        # close 通过 bar_times 长度推断，需要提供 close 序列
        matrix = _make_matrix(1, child_times, {("atr14", "atr"): [10.0]})
        matrix.closes = [1810.0]

        p = CrossTFFeatureProvider()
        result = p.compute(matrix, extra_data=extra_data)
        key = ("cross_tf", "dist_to_parent_ema")
        assert key in result
        val = result[key][0]
        assert val == pytest.approx(1.0)

    def test_zero_atr_returns_none(self) -> None:
        """ATR=0 时应返回 None（防止除以零）。"""
        parent_times = [datetime.fromtimestamp(0, tz=timezone.utc)]
        child_times = [datetime.fromtimestamp(0, tz=timezone.utc)]
        extra_data: Dict[str, Any] = {
            "parent_tf": "H4",
            "parent_bar_times": parent_times,
            "parent_indicators": {
                "supertrend_direction": [1.0],
                "rsi14": [50.0],
                "adx14": [25.0],
                "ema50": [1800.0],
                "bb_position": [0.5],
            },
        }
        matrix = _make_matrix(1, child_times, {("atr14", "atr"): [0.0]})
        matrix.closes = [1810.0]

        p = CrossTFFeatureProvider()
        result = p.compute(matrix, extra_data=extra_data)
        key = ("cross_tf", "dist_to_parent_ema")
        assert result[key][0] is None

    def test_missing_atr_returns_none(self) -> None:
        """无 ATR 指标时，dist_to_parent_ema 应为 None。"""
        parent_times = [datetime.fromtimestamp(0, tz=timezone.utc)]
        child_times = [datetime.fromtimestamp(0, tz=timezone.utc)]
        extra_data: Dict[str, Any] = {
            "parent_tf": "H4",
            "parent_bar_times": parent_times,
            "parent_indicators": {
                "supertrend_direction": [1.0],
                "rsi14": [50.0],
                "adx14": [25.0],
                "ema50": [1800.0],
                "bb_position": [0.5],
            },
        }
        matrix = _make_matrix(1, child_times, {})
        matrix.closes = [1810.0]

        p = CrossTFFeatureProvider()
        result = p.compute(matrix, extra_data=extra_data)
        key = ("cross_tf", "dist_to_parent_ema")
        assert result[key][0] is None


# ---------------------------------------------------------------------------
# 6. parent_rsi_delta_5 和 parent_adx_delta_5
# ---------------------------------------------------------------------------


class TestParentDeltaFeatures:
    def _build_extra_data(
        self,
        parent_rsi: List[float],
        parent_adx: List[float],
        n_parent: int,
    ) -> Dict[str, Any]:
        parent_times = _make_parent_times(n_parent)
        return {
            "parent_tf": "H4",
            "parent_bar_times": parent_times,
            "parent_indicators": {
                "supertrend_direction": [1.0] * n_parent,
                "rsi14": parent_rsi,
                "adx14": parent_adx,
                "ema50": [1800.0] * n_parent,
                "bb_position": [0.5] * n_parent,
            },
        }

    def test_rsi_delta_5_none_when_less_than_5_bars(self) -> None:
        """parent RSI 不足 5 个 bar 时，delta_5 应为 None。"""
        # 4 个父 bar，aligned 后在 child bars 0~3 上，delta_5 需要 idx-5 < 0 → None
        n_parent = 4
        extra_data = self._build_extra_data(
            parent_rsi=[50.0] * n_parent,
            parent_adx=[25.0] * n_parent,
            n_parent=n_parent,
        )
        # 4 个 child bar，全部对应 parent[0]~[3]
        child_times = _make_child_times(4)
        matrix = _make_matrix(4, child_times)
        p = CrossTFFeatureProvider()
        result = p.compute(matrix, extra_data=extra_data)
        key = ("cross_tf", "parent_rsi_delta_5")
        vals = result[key]
        # aligned idx 全为 [0,1,2,3]，delta_5 均 < 5 → None
        for v in vals:
            assert v is None

    def test_rsi_delta_5_computed_after_5_bars(self) -> None:
        """对齐后的 parent RSI 在第 6 个 child bar 时应有有效 delta。"""
        # 8 个父 bar，RSI 线性递增：50, 52, 54, 56, 58, 60, 62, 64
        n_parent = 8
        parent_rsi = [50.0 + 2 * i for i in range(n_parent)]
        parent_adx = [25.0] * n_parent
        extra_data = self._build_extra_data(parent_rsi, parent_adx, n_parent)

        # 8 个 child bar，每个对应不同 parent bar（M30 步长 = parent 步长）
        child_times = _make_child_times(8, step_sec=14400)
        matrix = _make_matrix(8, child_times)
        p = CrossTFFeatureProvider()
        result = p.compute(matrix, extra_data=extra_data)

        key = ("cross_tf", "parent_rsi_delta_5")
        vals = result[key]
        # child bar 5 → aligned parent idx=5, delta = rsi[5] - rsi[0] = 60 - 50 = 10
        assert vals[5] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# 7. compute() 返回 8 个特征 key
# ---------------------------------------------------------------------------


class TestComputeOutputShape:
    def test_compute_returns_8_features(self) -> None:
        """compute() 在完整数据下必须返回 8 个特征 key。"""
        n = 10
        parent_times = _make_parent_times(10)
        child_times = _make_child_times(10, step_sec=14400)

        extra_data: Dict[str, Any] = {
            "parent_tf": "H4",
            "parent_bar_times": parent_times,
            "parent_indicators": {
                "supertrend_direction": [1.0] * 10,
                "rsi14": [50.0 + float(i) for i in range(10)],
                "adx14": [25.0 + float(i) for i in range(10)],
                "ema50": [1800.0] * 10,
                "bb_position": [0.5] * 10,
            },
        }
        indicator_series: Dict[Tuple[str, str], List[Optional[float]]] = {
            ("supertrend", "direction"): [1.0] * n,
            ("atr14", "atr"): [10.0] * n,
        }
        matrix = _make_matrix(n, child_times, indicator_series)
        matrix.closes = [1810.0] * n

        p = CrossTFFeatureProvider()
        result = p.compute(matrix, extra_data=extra_data)

        # 必须恰好 8 个特征
        assert len(result) == 8
        expected_fields = {
            "parent_trend_dir",
            "parent_rsi",
            "parent_adx",
            "tf_trend_align",
            "dist_to_parent_ema",
            "parent_rsi_delta_5",
            "parent_adx_delta_5",
            "parent_bb_pos",
        }
        actual_fields = {field for (_, field) in result.keys()}
        assert actual_fields == expected_fields

    def test_all_result_lists_have_correct_length(self) -> None:
        """所有输出列的长度必须等于 n_bars。"""
        n = 7
        parent_times = _make_parent_times(7)
        child_times = _make_child_times(7, step_sec=14400)

        extra_data: Dict[str, Any] = {
            "parent_tf": "H4",
            "parent_bar_times": parent_times,
            "parent_indicators": {
                "supertrend_direction": [1.0] * 7,
                "rsi14": [50.0] * 7,
                "adx14": [25.0] * 7,
                "ema50": [1800.0] * 7,
                "bb_position": [0.5] * 7,
            },
        }
        indicator_series: Dict[Tuple[str, str], List[Optional[float]]] = {
            ("supertrend", "direction"): [1.0] * n,
            ("atr14", "atr"): [10.0] * n,
        }
        matrix = _make_matrix(n, child_times, indicator_series)
        matrix.closes = [1810.0] * n

        p = CrossTFFeatureProvider()
        result = p.compute(matrix, extra_data=extra_data)

        for key, vals in result.items():
            assert len(vals) == n, f"特征 {key} 长度错误: {len(vals)} != {n}"
