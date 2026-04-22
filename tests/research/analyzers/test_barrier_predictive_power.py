"""analyze_barrier_predictive_power 契约 + 行为测试。

守护 Gap 2b：IC 基于 Triple-Barrier 真实出场收益，替代短 forward_return 语义。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pytest

from src.research.analyzers.barrier_predictive_power import (
    analyze_barrier_predictive_power,
)
from src.research.core.barrier import BarrierOutcome
from src.research.core.config import OverfittingConfig, PredictivePowerConfig
from src.research.core.contracts import IndicatorBarrierPredictiveResult
from src.research.core.data_matrix import DataMatrix
from src.signals.evaluation.regime import RegimeType


def _make_matrix_with_barrier(
    n: int,
    feature_values: List[Optional[float]],
    outcomes_long: List[Optional[BarrierOutcome]],
    *,
    barrier_key: tuple = (1.5, 2.5, 40),
    train_end_idx: Optional[int] = None,
    split_idx: Optional[int] = None,
) -> DataMatrix:
    """构造最小 DataMatrix，仅填关键字段供 analyzer 消费。"""
    if train_end_idx is None:
        train_end_idx = int(n * 0.7)
    if split_idx is None:
        split_idx = train_end_idx
    return DataMatrix(
        symbol="XAUUSD",
        timeframe="H1",
        n_bars=n,
        bar_times=[
            datetime(2026, 1, 1, tzinfo=timezone.utc) for _ in range(n)
        ],
        opens=[2000.0] * n,
        highs=[2001.0] * n,
        lows=[1999.0] * n,
        closes=[2000.0] * n,
        volumes=[100.0] * n,
        indicators=[{"rsi14": {"rsi": v}} if v is not None else {} for v in feature_values],
        regimes=[RegimeType.UNCERTAIN for _ in range(n)],
        soft_regimes=[None] * n,
        forward_returns={},
        indicator_series={("rsi14", "rsi"): list(feature_values)},
        train_end_idx=train_end_idx,
        split_idx=split_idx,
        barrier_returns_long={barrier_key: outcomes_long},
        barrier_returns_short={barrier_key: [None] * n},
    )


class TestAnalyzerBehavior:
    def test_empty_barrier_returns_returns_empty_list(self) -> None:
        matrix = DataMatrix(
            symbol="XAUUSD",
            timeframe="H1",
            n_bars=100,
            bar_times=[datetime(2026, 1, 1, tzinfo=timezone.utc)] * 100,
            opens=[0.0] * 100,
            highs=[0.0] * 100,
            lows=[0.0] * 100,
            closes=[0.0] * 100,
            volumes=[0.0] * 100,
            indicators=[{} for _ in range(100)],
            regimes=[RegimeType.UNCERTAIN] * 100,
            soft_regimes=[None] * 100,
            forward_returns={},
            indicator_series={},
            train_end_idx=70,
            split_idx=70,
            barrier_returns_long={},
            barrier_returns_short={},
        )
        results = analyze_barrier_predictive_power(matrix)
        assert results == []

    def test_below_min_samples_skipped(self) -> None:
        """样本数低于 min_samples 的组合不产出结果。"""
        n = 20  # < default min_samples=30
        matrix = _make_matrix_with_barrier(
            n=n,
            feature_values=[float(i) for i in range(n)],
            outcomes_long=[
                BarrierOutcome(barrier="tp", return_pct=0.02, bars_held=5)
                for _ in range(n)
            ],
            train_end_idx=n,
            split_idx=n,
        )
        results = analyze_barrier_predictive_power(
            matrix,
            config=PredictivePowerConfig(n_permutations=0),
            overfitting_config=OverfittingConfig(min_samples=30),
        )
        # n=20 < min_samples=30 → 无结果
        long_results = [r for r in results if r.direction == "long"]
        assert len(long_results) == 0

    def test_positive_correlation_reflects_barrier_ic(self) -> None:
        """feature 与 barrier return 线性正相关 → IC > 0。"""
        n = 100
        rng = np.random.default_rng(42)
        # feature = 0..99；return 随 feature 递增（正相关）
        feats = [float(i) for i in range(n)]
        outcomes: List[Optional[BarrierOutcome]] = []
        for i in range(n):
            # 高 feature → tp 命中；低 feature → sl 命中
            if i >= 50:
                outcomes.append(
                    BarrierOutcome(
                        barrier="tp",
                        return_pct=0.01 + rng.normal(0, 0.001),
                        bars_held=int(rng.integers(3, 15)),
                    )
                )
            else:
                outcomes.append(
                    BarrierOutcome(
                        barrier="sl",
                        return_pct=-0.01 + rng.normal(0, 0.001),
                        bars_held=int(rng.integers(3, 15)),
                    )
                )

        matrix = _make_matrix_with_barrier(
            n=n,
            feature_values=feats,
            outcomes_long=outcomes,
            train_end_idx=n,
            split_idx=n,
        )
        results = analyze_barrier_predictive_power(
            matrix,
            config=PredictivePowerConfig(
                n_permutations=0, significance_level=0.05
            ),
            overfitting_config=OverfittingConfig(min_samples=30),
        )
        long_results = [r for r in results if r.direction == "long"]
        assert len(long_results) >= 1
        # IC 强正相关（阶梯函数的 Spearman 受 ties 影响，> 0.7 即显著）
        best = long_results[0]
        assert best.information_coefficient > 0.7
        assert best.n_samples == n

    def test_exit_breakdown_sums_to_one(self) -> None:
        """tp/sl/time rates 应接近 1.0（容忍 rounding）。"""
        n = 50
        feats = [float(i) for i in range(n)]
        outcomes = (
            [BarrierOutcome(barrier="tp", return_pct=0.02, bars_held=5)] * 20
            + [BarrierOutcome(barrier="sl", return_pct=-0.01, bars_held=3)] * 20
            + [BarrierOutcome(barrier="time", return_pct=0.005, bars_held=40)] * 10
        )
        matrix = _make_matrix_with_barrier(
            n=n,
            feature_values=feats,
            outcomes_long=outcomes,
            train_end_idx=n,
            split_idx=n,
        )
        results = analyze_barrier_predictive_power(
            matrix,
            config=PredictivePowerConfig(n_permutations=0),
            overfitting_config=OverfittingConfig(min_samples=10),
        )
        long_results = [r for r in results if r.direction == "long"]
        assert len(long_results) >= 1
        r = long_results[0]
        assert r.tp_hit_rate == pytest.approx(0.4)
        assert r.sl_hit_rate == pytest.approx(0.4)
        assert r.time_exit_rate == pytest.approx(0.2)

    def test_none_outcomes_filtered(self) -> None:
        """outcome 为 None 的 bar 不计入样本。"""
        n = 60
        feats = [float(i) for i in range(n)]
        # 前 30 个 None，后 30 个有效（return_pct 有变化以产生 std > 0）
        outcomes: List[Optional[BarrierOutcome]] = [None] * 30 + [
            BarrierOutcome(
                barrier="tp",
                return_pct=0.01 + 0.0001 * i,
                bars_held=5,
            )
            for i in range(30)
        ]
        matrix = _make_matrix_with_barrier(
            n=n,
            feature_values=feats,
            outcomes_long=outcomes,
            train_end_idx=n,
            split_idx=n,
        )
        results = analyze_barrier_predictive_power(
            matrix,
            config=PredictivePowerConfig(n_permutations=0),
            overfitting_config=OverfittingConfig(min_samples=20),
        )
        long_results = [r for r in results if r.direction == "long"]
        assert len(long_results) >= 1
        assert long_results[0].n_samples == 30


class TestContractShape:
    def test_contract_to_dict_keys(self) -> None:
        result = IndicatorBarrierPredictiveResult(
            indicator_name="rsi14",
            field_name="rsi",
            regime=None,
            direction="long",
            barrier_key=(1.5, 2.5, 40),
            n_samples=100,
            pearson_r=0.3,
            spearman_rho=0.35,
            information_coefficient=0.35,
            p_value=0.01,
            permutation_p_value=0.02,
            is_significant=True,
            tp_hit_rate=0.5,
            sl_hit_rate=0.3,
            time_exit_rate=0.2,
            mean_bars_held=15.5,
            mean_return_pct=0.008,
        )
        d = result.to_dict()
        expected_keys = {
            "indicator",
            "regime",
            "direction",
            "barrier",
            "n_samples",
            "pearson_r",
            "spearman_rho",
            "ic",
            "p_value",
            "permutation_p_value",
            "is_significant",
            "tp_hit_rate",
            "sl_hit_rate",
            "time_exit_rate",
            "mean_bars_held",
            "mean_return_pct",
        }
        assert set(d.keys()) == expected_keys
        assert d["barrier"] == "sl=1.5/tp=2.5/time=40"

    def test_direction_values(self) -> None:
        for direction in ("long", "short"):
            IndicatorBarrierPredictiveResult(
                indicator_name="x",
                field_name="y",
                regime=None,
                direction=direction,
                barrier_key=(1.0, 2.0, 20),
                n_samples=30,
                pearson_r=0.0,
                spearman_rho=0.0,
                information_coefficient=0.0,
                p_value=1.0,
                permutation_p_value=None,
                is_significant=False,
                tp_hit_rate=0.0,
                sl_hit_rate=0.0,
                time_exit_rate=0.0,
                mean_bars_held=0.0,
                mean_return_pct=0.0,
            )
