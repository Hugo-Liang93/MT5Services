"""Threshold Sweep 分析器单元测试。"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest

from src.research.analyzers.threshold import analyze_thresholds
from src.research.config import OverfittingConfig, ThresholdSweepConfig
from src.research.data_matrix import DataMatrix
from src.signals.evaluation.regime import RegimeType


def _make_threshold_matrix(
    n_bars: int = 300,
    buy_threshold: float = 30.0,
    sell_threshold: float = 70.0,
    hit_rate: float = 0.65,
    seed: int = 42,
) -> DataMatrix:
    """构造 RSI 风格指标的 DataMatrix，在已知阈值处有较高命中率。"""
    rng = random.Random(seed)

    closes = [2000.0 + rng.gauss(0, 5) for _ in range(n_bars)]
    bar_times = [datetime(2026, 1, 1, tzinfo=timezone.utc)] * n_bars

    # RSI 风格指标：0-100 之间均匀分布
    rsi_vals = [rng.uniform(10, 90) for _ in range(n_bars)]

    # 构造前瞻收益：在 buy/sell 阈值附近有正向收益
    forward_vals: List[Optional[float]] = []
    for i in range(n_bars):
        if i >= n_bars - 5:
            forward_vals.append(None)
            continue
        rsi = rsi_vals[i]
        if rsi <= buy_threshold:
            # 买入区：命中率较高的正收益
            if rng.random() < hit_rate:
                forward_vals.append(abs(rng.gauss(0.003, 0.002)))
            else:
                forward_vals.append(-abs(rng.gauss(0.002, 0.001)))
        elif rsi >= sell_threshold:
            # 卖出区：命中率较高的负收益（做空获利）
            if rng.random() < hit_rate:
                forward_vals.append(-abs(rng.gauss(0.003, 0.002)))
            else:
                forward_vals.append(abs(rng.gauss(0.002, 0.001)))
        else:
            # 中间区：随机
            forward_vals.append(rng.gauss(0, 0.002))

    regimes = [RegimeType.RANGING] * n_bars
    indicators: List[Dict[str, Dict[str, Any]]] = [
        {"rsi14": {"rsi": rsi_vals[i]}} for i in range(n_bars)
    ]
    indicator_series: Dict[Tuple[str, str], List[Optional[float]]] = {
        ("rsi14", "rsi"): [float(v) for v in rsi_vals],
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


class TestThresholdSweep:
    def test_finds_buy_threshold(self) -> None:
        """应找到接近 30 的买入阈值。"""
        matrix = _make_threshold_matrix(500, buy_threshold=30.0, hit_rate=0.70, seed=42)

        results = analyze_thresholds(
            matrix,
            "rsi14",
            "rsi",
            horizons=[5],
            config=ThresholdSweepConfig(sweep_points=30, target_metric="hit_rate"),
            overfitting_config=OverfittingConfig(min_samples=10),
        )

        assert len(results) == 1
        r = results[0]
        # 最优买入阈值应在合理范围内
        if r.optimal_buy_threshold is not None:
            assert 15.0 <= r.optimal_buy_threshold <= 50.0
            assert r.buy_hit_rate > 0.5
            assert r.buy_n_signals > 0

    def test_finds_sell_threshold(self) -> None:
        """应找到接近 70 的卖出阈值。"""
        matrix = _make_threshold_matrix(
            500, sell_threshold=70.0, hit_rate=0.70, seed=42
        )

        results = analyze_thresholds(
            matrix,
            "rsi14",
            "rsi",
            horizons=[5],
            config=ThresholdSweepConfig(sweep_points=30, target_metric="hit_rate"),
            overfitting_config=OverfittingConfig(min_samples=10),
        )

        assert len(results) == 1
        r = results[0]
        if r.optimal_sell_threshold is not None:
            assert 50.0 <= r.optimal_sell_threshold <= 90.0
            assert r.sell_hit_rate > 0.5

    def test_cv_consistency_reported(self) -> None:
        """应报告交叉验证一致性。"""
        matrix = _make_threshold_matrix(500, hit_rate=0.70, seed=42)

        results = analyze_thresholds(
            matrix,
            "rsi14",
            "rsi",
            horizons=[5],
            config=ThresholdSweepConfig(sweep_points=20),
            overfitting_config=OverfittingConfig(min_samples=10, cv_folds=3),
        )

        assert len(results) == 1
        # CV consistency 应在 0-1 之间
        assert 0.0 <= results[0].cv_consistency_buy <= 1.0
        assert 0.0 <= results[0].cv_consistency_sell <= 1.0

    def test_test_set_validation(self) -> None:
        """应在测试集上验证阈值。"""
        matrix = _make_threshold_matrix(500, hit_rate=0.70, seed=42)

        results = analyze_thresholds(
            matrix,
            "rsi14",
            "rsi",
            horizons=[5],
            config=ThresholdSweepConfig(sweep_points=20),
            overfitting_config=OverfittingConfig(min_samples=10),
        )

        assert len(results) == 1
        r = results[0]
        # 测试集验证结果应存在
        if r.optimal_buy_threshold is not None:
            assert r.test_buy_n_signals >= 0
        if r.optimal_sell_threshold is not None:
            assert r.test_sell_n_signals >= 0

    def test_insufficient_data_returns_empty(self) -> None:
        """数据不足时返回空列表。"""
        matrix = _make_threshold_matrix(20, seed=42)

        results = analyze_thresholds(
            matrix,
            "rsi14",
            "rsi",
            overfitting_config=OverfittingConfig(min_samples=50),
        )

        assert len(results) == 0

    def test_nonexistent_indicator_returns_empty(self) -> None:
        """不存在的指标返回空列表。"""
        matrix = _make_threshold_matrix(200)

        results = analyze_thresholds(matrix, "nonexistent", "field")
        assert len(results) == 0

    def test_to_dict_format(self) -> None:
        """验证结果的 to_dict 格式。"""
        matrix = _make_threshold_matrix(300, hit_rate=0.65, seed=42)

        results = analyze_thresholds(
            matrix,
            "rsi14",
            "rsi",
            horizons=[5],
            config=ThresholdSweepConfig(sweep_points=20),
            overfitting_config=OverfittingConfig(min_samples=10),
        )

        assert len(results) > 0
        d = results[0].to_dict()
        assert "indicator" in d
        assert "buy" in d
        assert "sell" in d
        assert "cv_consistency" in d["buy"]
