"""Intrabar 派生特征单元测试。

覆盖 5 个 intrabar 特征的 per-bar + batch 两条路径，
以及 DataMatrix.child_bars / child_tf 字段。
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest

from src.clients.mt5_market import OHLC
from src.research.core.data_matrix import DataMatrix
from src.research.features.engineer import (
    FeatureEngineer,
    _batch_child_bar_consensus,
    _batch_child_bar_count_ratio,
    _batch_child_range_acceleration,
    _batch_child_volume_front_weight,
    _batch_intrabar_momentum_shift,
    _child_bar_consensus,
    _child_bar_count_ratio,
    _child_range_acceleration,
    _child_volume_front_weight,
    _intrabar_momentum_shift,
    build_default_engineer,
)
from src.signals.evaluation.regime import RegimeType


def _make_child_bar(
    t: datetime,
    o: float,
    h: float,
    l: float,
    c: float,
    v: float = 100.0,
) -> OHLC:
    return OHLC(
        symbol="XAUUSD",
        timeframe="M5",
        time=t,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=v,
    )


def _make_matrix(
    n: int = 5,
    child_bars: Optional[Dict[int, List[OHLC]]] = None,
    child_tf: str = "M5",
    opens: Optional[List[float]] = None,
    closes: Optional[List[float]] = None,
) -> DataMatrix:
    """构建最小 DataMatrix，仅含 intrabar 特征需要的字段。"""
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    _opens = opens or [2000.0 + i for i in range(n)]
    _closes = closes or [2000.5 + i for i in range(n)]
    return DataMatrix(
        symbol="XAUUSD",
        timeframe="H1",
        n_bars=n,
        bar_times=[base + timedelta(hours=i) for i in range(n)],
        opens=_opens,
        highs=[max(o, c) + 1.0 for o, c in zip(_opens, _closes)],
        lows=[min(o, c) - 1.0 for o, c in zip(_opens, _closes)],
        closes=_closes,
        volumes=[100.0] * n,
        indicators=[{}] * n,
        regimes=[RegimeType.TRENDING] * n,
        soft_regimes=[None] * n,
        forward_returns={},
        indicator_series={},
        child_bars=child_bars or {},
        child_tf=child_tf,
    )


def _bullish_children(base_time: datetime, count: int = 6) -> List[OHLC]:
    """全阳线子 bars。"""
    return [
        _make_child_bar(
            base_time + timedelta(minutes=5 * i),
            2000.0 + i,
            2002.0 + i,
            1999.0 + i,
            2001.0 + i,
            50.0 + i,
        )
        for i in range(count)
    ]


def _mixed_children(base_time: datetime) -> List[OHLC]:
    """阳-阴交替子 bars。"""
    bars = []
    for i in range(6):
        t = base_time + timedelta(minutes=5 * i)
        if i % 2 == 0:
            bars.append(_make_child_bar(t, 2000.0, 2002.0, 1999.0, 2001.0, 50.0))
        else:
            bars.append(_make_child_bar(t, 2001.0, 2002.0, 1999.0, 2000.0, 50.0))
    return bars


# ── child_bar_consensus ──────────────────────────────────────────


class TestChildBarConsensus:
    def test_all_same_direction(self) -> None:
        """全阳线父 bar + 全阳线子 bars → consensus = 1.0。"""
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        matrix = _make_matrix(
            n=1,
            opens=[2000.0],
            closes=[2005.0],  # 阳线
            child_bars={0: _bullish_children(base)},
        )
        result = _child_bar_consensus(matrix, 0)
        assert result == 1.0

    def test_mixed_direction(self) -> None:
        """交替子 bars → consensus ~0.5。"""
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        matrix = _make_matrix(
            n=1,
            opens=[2000.0],
            closes=[2005.0],
            child_bars={0: _mixed_children(base)},
        )
        result = _child_bar_consensus(matrix, 0)
        assert result is not None
        assert 0.4 <= result <= 0.6

    def test_no_child_bars(self) -> None:
        """无子 bars → None。"""
        matrix = _make_matrix(n=1)
        assert _child_bar_consensus(matrix, 0) is None

    def test_batch_matches_per_bar(self) -> None:
        """batch 与 per-bar 结果一致。"""
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        matrix = _make_matrix(
            n=3,
            opens=[2000.0, 2001.0, 2002.0],
            closes=[2005.0, 2006.0, 2007.0],
            child_bars={
                0: _bullish_children(base),
                2: _mixed_children(base + timedelta(hours=2)),
            },
        )
        batch = _batch_child_bar_consensus(matrix)
        for i in range(matrix.n_bars):
            per_bar = _child_bar_consensus(matrix, i)
            assert batch[i] == per_bar


# ── child_range_acceleration ─────────────────────────────────────


class TestChildRangeAcceleration:
    def test_expanding_range(self) -> None:
        """后半段 range 更大 → 正值。"""
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        children = []
        for i in range(6):
            t = base + timedelta(minutes=5 * i)
            spread = 1.0 + i * 0.5  # 递增 range
            children.append(
                _make_child_bar(
                    t, 2000.0, 2000.0 + spread, 2000.0 - 0.1, 2000.0 + 0.5, 50.0
                )
            )

        matrix = _make_matrix(n=1, child_bars={0: children})
        result = _child_range_acceleration(matrix, 0)
        assert result is not None
        assert result > 0  # 后半段 range 更大

    def test_too_few_children(self) -> None:
        """不足 4 根 → None。"""
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        matrix = _make_matrix(n=1, child_bars={0: _bullish_children(base, count=3)})
        assert _child_range_acceleration(matrix, 0) is None

    def test_batch_matches_per_bar(self) -> None:
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        matrix = _make_matrix(n=2, child_bars={0: _bullish_children(base, count=6)})
        batch = _batch_child_range_acceleration(matrix)
        for i in range(matrix.n_bars):
            assert batch[i] == _child_range_acceleration(matrix, i)


# ── intrabar_momentum_shift ──────────────────────────────────────


class TestIntrabarMomentumShift:
    def test_monotonic_no_shift(self) -> None:
        """单调递增 → 0 次方向切换。"""
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        children = [
            _make_child_bar(
                base + timedelta(minutes=5 * i), 2000 + i, 2002 + i, 1999 + i, 2001 + i
            )
            for i in range(6)
        ]
        matrix = _make_matrix(n=1, child_bars={0: children})
        result = _intrabar_momentum_shift(matrix, 0)
        assert result is not None
        assert result == 0.0

    def test_alternating_shift(self) -> None:
        """交替涨跌 → 高切换率。"""
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        children = []
        for i in range(6):
            t = base + timedelta(minutes=5 * i)
            if i % 2 == 0:
                children.append(_make_child_bar(t, 2000.0, 2002.0, 1999.0, 2001.0))
            else:
                children.append(_make_child_bar(t, 2001.0, 2002.0, 1999.0, 2000.0))
        matrix = _make_matrix(n=1, child_bars={0: children})
        result = _intrabar_momentum_shift(matrix, 0)
        assert result is not None
        assert result > 0.5  # 多次方向切换

    def test_batch_matches_per_bar(self) -> None:
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        matrix = _make_matrix(n=2, child_bars={0: _bullish_children(base)})
        batch = _batch_intrabar_momentum_shift(matrix)
        for i in range(matrix.n_bars):
            assert batch[i] == _intrabar_momentum_shift(matrix, i)


# ── child_volume_front_weight ────────────────────────────────────


class TestChildVolumeFrontWeight:
    def test_uniform_volume(self) -> None:
        """均匀 volume → ~0.5。"""
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        children = [
            _make_child_bar(
                base + timedelta(minutes=5 * i), 2000, 2002, 1999, 2001, 100.0
            )
            for i in range(6)
        ]
        matrix = _make_matrix(n=1, child_bars={0: children})
        result = _child_volume_front_weight(matrix, 0)
        assert result is not None
        assert abs(result - 0.5) < 0.01

    def test_front_loaded(self) -> None:
        """前半 volume 大 → > 0.5。"""
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        children = []
        for i in range(6):
            v = 200.0 if i < 3 else 50.0
            children.append(
                _make_child_bar(
                    base + timedelta(minutes=5 * i), 2000, 2002, 1999, 2001, v
                )
            )
        matrix = _make_matrix(n=1, child_bars={0: children})
        result = _child_volume_front_weight(matrix, 0)
        assert result is not None
        assert result > 0.6

    def test_batch_matches_per_bar(self) -> None:
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        matrix = _make_matrix(n=2, child_bars={0: _bullish_children(base)})
        batch = _batch_child_volume_front_weight(matrix)
        for i in range(matrix.n_bars):
            assert batch[i] == _child_volume_front_weight(matrix, i)


# ── child_bar_count_ratio ────────────────────────────────────────


class TestChildBarCountRatio:
    def test_full_coverage(self) -> None:
        """12 根 M5 / H1 → ratio = 1.0。"""
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        children = _bullish_children(base, count=12)
        matrix = _make_matrix(n=1, child_bars={0: children}, child_tf="M5")
        result = _child_bar_count_ratio(matrix, 0)
        assert result == 1.0

    def test_partial_coverage(self) -> None:
        """6 根 / 12 期望 = 0.5。"""
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        children = _bullish_children(base, count=6)
        matrix = _make_matrix(n=1, child_bars={0: children}, child_tf="M5")
        result = _child_bar_count_ratio(matrix, 0)
        assert result is not None
        assert abs(result - 0.5) < 0.01

    def test_no_child_tf(self) -> None:
        """无 child_tf → None。"""
        matrix = _make_matrix(
            n=1,
            child_bars={
                0: _bullish_children(datetime(2025, 1, 1, tzinfo=timezone.utc))
            },
            child_tf="",
        )
        assert _child_bar_count_ratio(matrix, 0) is None

    def test_missing_index(self) -> None:
        """bar index 无子 bars → None。"""
        matrix = _make_matrix(n=2, child_tf="M5")
        assert _child_bar_count_ratio(matrix, 0) is None

    def test_batch_matches_per_bar(self) -> None:
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        matrix = _make_matrix(
            n=3,
            child_bars={0: _bullish_children(base, 12), 1: _bullish_children(base, 6)},
            child_tf="M5",
        )
        batch = _batch_child_bar_count_ratio(matrix)
        for i in range(matrix.n_bars):
            assert batch[i] == _child_bar_count_ratio(matrix, i)


# ── FeatureEngineer 集成测试 ─────────────────────────────────────


class TestEngineerIntrabarIntegration:
    def test_intrabar_features_registered(self) -> None:
        """build_default_engineer 包含 5 个 intrabar 特征。"""
        eng = build_default_engineer()
        intra_names = [
            "child_bar_consensus",
            "child_range_acceleration",
            "intrabar_momentum_shift",
            "child_volume_front_weight",
            "child_bar_count_ratio",
        ]
        for name in intra_names:
            defn = eng.definition(name)
            assert defn is not None, f"Feature {name} not registered"
            assert defn.group == "derived_intrabar"

    def test_enrich_with_child_bars(self) -> None:
        """enrich() 在有 child_bars 时计算 intrabar 特征。"""
        eng = build_default_engineer()
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        matrix = _make_matrix(
            n=3,
            opens=[2000.0, 2001.0, 2002.0],
            closes=[2005.0, 2006.0, 2007.0],
            child_bars={
                0: _bullish_children(base, count=12),
                1: _bullish_children(base + timedelta(hours=1), count=12),
            },
            child_tf="M5",
        )
        enriched = eng.enrich(
            matrix,
            feature_names=["child_bar_consensus", "child_bar_count_ratio"],
        )
        # key 格式 = (group, name)
        assert ("derived_intrabar", "child_bar_consensus") in enriched.indicator_series
        assert (
            "derived_intrabar",
            "child_bar_count_ratio",
        ) in enriched.indicator_series
        # bar 0 有子 bars → 有值
        consensus_series = enriched.indicator_series[
            ("derived_intrabar", "child_bar_consensus")
        ]
        assert consensus_series[0] is not None
        # bar 2 无子 bars → None
        assert consensus_series[2] is None

    def test_enrich_without_child_bars_skips(self) -> None:
        """enrich() 在无 child_bars 时跳过 intrabar 特征（不浪费计算）。"""
        eng = build_default_engineer()
        matrix = _make_matrix(n=3)
        enriched = eng.enrich(
            matrix,
            feature_names=["child_bar_consensus"],
        )
        # child_bars 为空 → 特征被跳过，不会出现在 indicator_series 中
        assert (
            "derived_intrabar",
            "child_bar_consensus",
        ) not in enriched.indicator_series
