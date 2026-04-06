"""Predictive Power 分析器单元测试 — 使用已知相关性的合成数据验证。"""

from __future__ import annotations

import math
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest

from src.research.analyzers.predictive_power import analyze_predictive_power
from src.research.config import OverfittingConfig, PredictivePowerConfig
from src.research.data_matrix import DataMatrix
from src.signals.evaluation.regime import RegimeType


def _make_correlated_matrix(
    n_bars: int = 300,
    correlation: float = 0.5,
    seed: int = 42,
) -> DataMatrix:
    """构造一个指标值与前瞻收益有已知相关性的 DataMatrix。

    indicator = base + noise
    forward_return = correlation * base + (1-correlation) * noise2
    """
    rng = random.Random(seed)

    closes = [2000.0 + i * 0.01 for i in range(n_bars)]
    bar_times = [datetime(2026, 1, 1, tzinfo=timezone.utc)] * n_bars

    # 生成相关的指标和前瞻收益
    base = [rng.gauss(0, 1) for _ in range(n_bars)]
    noise1 = [rng.gauss(0, 0.5) for _ in range(n_bars)]
    noise2 = [rng.gauss(0, 1) for _ in range(n_bars)]

    indicator_vals = [50.0 + 10.0 * (b + n) for b, n in zip(base, noise1)]
    forward_vals: List[Optional[float]] = [
        0.001 * (correlation * b + (1 - abs(correlation)) * n)
        for b, n in zip(base, noise2)
    ]
    # 最后几个设为 None
    for i in range(n_bars - 5, n_bars):
        forward_vals[i] = None

    # 不相关的指标（常量 + 噪声）
    uncorr_vals = [25.0 + rng.gauss(0, 0.1) for _ in range(n_bars)]

    regimes = [RegimeType.TRENDING] * n_bars
    indicators: List[Dict[str, Dict[str, Any]]] = [
        {
            "test_ind": {"value": indicator_vals[i]},
            "noise_ind": {"value": uncorr_vals[i]},
        }
        for i in range(n_bars)
    ]

    indicator_series: Dict[Tuple[str, str], List[Optional[float]]] = {
        ("test_ind", "value"): [float(v) for v in indicator_vals],
        ("noise_ind", "value"): [float(v) for v in uncorr_vals],
    }

    split_idx = int(n_bars * 0.7)

    return DataMatrix(
        symbol="XAUUSD",
        timeframe="H1",
        n_bars=n_bars,
        bar_times=bar_times,
        opens=closes,
        highs=closes,
        lows=closes,
        closes=closes,
        volumes=[100.0] * n_bars,
        indicators=indicators,
        regimes=regimes,
        soft_regimes=[None] * n_bars,
        forward_returns={5: forward_vals},
        indicator_series=indicator_series,
        split_idx=split_idx,
    )


class TestPredictivePower:
    def test_detects_correlated_indicator(self) -> None:
        """高相关指标应排在不相关指标前面。"""
        matrix = _make_correlated_matrix(300, correlation=0.5)

        results = analyze_predictive_power(
            matrix,
            horizons=[5],
            config=PredictivePowerConfig(significance_level=0.05, per_regime=False),
            overfitting_config=OverfittingConfig(
                min_samples=20,
                bonferroni_correction=False,  # 简化测试
            ),
        )

        assert len(results) >= 2
        # 第一个应是相关指标
        top = results[0]
        assert top.indicator_name == "test_ind"
        assert abs(top.information_coefficient) > 0.1

        # noise_ind 应有较小的 IC
        noise_result = next(r for r in results if r.indicator_name == "noise_ind")
        assert abs(noise_result.information_coefficient) < abs(
            top.information_coefficient
        )

    def test_significance_detection(self) -> None:
        """高相关指标应被标记为显著。"""
        matrix = _make_correlated_matrix(300, correlation=0.6)

        results = analyze_predictive_power(
            matrix,
            horizons=[5],
            config=PredictivePowerConfig(significance_level=0.05, per_regime=False),
            overfitting_config=OverfittingConfig(
                min_samples=20,
                bonferroni_correction=False,
            ),
        )

        corr_result = next(r for r in results if r.indicator_name == "test_ind")
        assert corr_result.is_significant
        assert corr_result.p_value < 0.05

    def test_insufficient_samples_filtered(self) -> None:
        """样本不足时不返回结果。"""
        matrix = _make_correlated_matrix(20, correlation=0.5)

        results = analyze_predictive_power(
            matrix,
            horizons=[5],
            overfitting_config=OverfittingConfig(min_samples=50),
        )

        assert len(results) == 0

    def test_constant_indicator_filtered(self) -> None:
        """常量指标应被过滤掉。"""
        matrix = _make_correlated_matrix(200, correlation=0.5)
        # 替换 noise_ind 为常量
        const_series = [25.0] * matrix.n_bars
        # 需要重建 DataMatrix 因为它是 frozen
        import dataclasses

        new_series = dict(matrix.indicator_series)
        new_series[("noise_ind", "value")] = const_series
        matrix2 = dataclasses.replace(matrix, indicator_series=new_series)

        results = analyze_predictive_power(
            matrix2,
            horizons=[5],
            indicator_fields=[("noise_ind", "value")],
            overfitting_config=OverfittingConfig(
                min_samples=20, bonferroni_correction=False
            ),
        )

        assert len(results) == 0

    def test_hit_rate_divergence(self) -> None:
        """正相关指标：above_median 命中率应高于 below_median。"""
        matrix = _make_correlated_matrix(500, correlation=0.6, seed=123)

        results = analyze_predictive_power(
            matrix,
            horizons=[5],
            indicator_fields=[("test_ind", "value")],
            config=PredictivePowerConfig(per_regime=False),
            overfitting_config=OverfittingConfig(
                min_samples=20, bonferroni_correction=False
            ),
        )

        assert len(results) == 1
        r = results[0]
        # 正相关：指标高 → 收益高 → above_median 命中率 > below_median
        assert r.hit_rate_above_median > r.hit_rate_below_median

    def test_per_regime_analysis(self) -> None:
        """per_regime=True 时应为每个 regime 生成独立结果。"""
        matrix = _make_correlated_matrix(300, correlation=0.5)

        results = analyze_predictive_power(
            matrix,
            horizons=[5],
            indicator_fields=[("test_ind", "value")],
            config=PredictivePowerConfig(per_regime=True),
            overfitting_config=OverfittingConfig(
                min_samples=20, bonferroni_correction=False
            ),
        )

        # 应有 all + trending 的结果（matrix 全是 trending）
        regimes_seen = {r.regime for r in results}
        assert None in regimes_seen  # all regimes
        assert "trending" in regimes_seen

    def test_to_dict_format(self) -> None:
        """验证结果的 to_dict 格式。"""
        matrix = _make_correlated_matrix(200, correlation=0.5)

        results = analyze_predictive_power(
            matrix,
            horizons=[5],
            config=PredictivePowerConfig(per_regime=False),
            overfitting_config=OverfittingConfig(
                min_samples=20, bonferroni_correction=False
            ),
        )

        assert len(results) > 0
        d = results[0].to_dict()
        assert "indicator" in d
        assert "ic" in d
        assert "p_value" in d
        assert "n_samples" in d
        assert isinstance(d["ic"], float)
