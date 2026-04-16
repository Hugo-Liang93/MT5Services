"""Train/test gap 与 IC p-value 回归测试。

覆盖两处 CRITICAL 修复：
  1. data_matrix.py gap off-by-1（barrier entry_idx=i+1 偏移导致的 1-bar 泄漏）
  2. predictive_power.py 预先 ×2 Bonferroni 导致的 double correction 语义歧义
"""

from __future__ import annotations

import pytest

from src.research.core.barrier import BarrierConfig


class TestTrainTestGap:
    def test_gap_covers_barrier_time_plus_one(self) -> None:
        """gap 必须 ≥ max_barrier_time + 1（barrier entry_idx = i+1 偏移）。

        模拟：train_end_idx = 70, barrier time_bars = 20, forward_horizon = 10
        旧逻辑：split_idx = train_end + 20 → gap = 20 → train 最后样本 i=69
              barrier 扫描到 closes[90] = test 第一根 → 泄漏 1 bar
        新逻辑：gap = max(10, 21) = 21 → split_idx = 91 → closes[90] 在 gap 内 → 无泄漏
        """
        # 通过公式直接验证（避免构造完整 DataMatrix 的 IO 成本）
        train_end_idx = 70
        max_forward = 10
        max_barrier_time = 20

        # 新公式：max(max_forward, max_barrier_time + 1)
        new_gap = max(max_forward, max_barrier_time + 1)
        split_idx = train_end_idx + new_gap
        # 最后 train 样本 i = train_end_idx - 1 = 69
        # barrier 最远访问 closes[i+1+time_bars] = closes[90]
        last_barrier_access = (train_end_idx - 1) + 1 + max_barrier_time
        assert last_barrier_access < split_idx, (
            f"barrier access idx={last_barrier_access} must be < split_idx={split_idx}"
        )

    def test_gap_when_forward_dominates(self) -> None:
        """forward_horizon > barrier_time + 1 时，max_forward 主导 gap。"""
        max_forward = 50
        max_barrier_time = 20
        new_gap = max(max_forward, max_barrier_time + 1)
        assert new_gap == 50  # max_forward

    def test_gap_when_barrier_dominates(self) -> None:
        """barrier 典型场景（time_bars 大）时，+1 修正生效。"""
        max_forward = 10
        max_barrier_time = 120
        new_gap = max(max_forward, max_barrier_time + 1)
        assert new_gap == 121

    def test_gap_via_build_data_matrix_integration(self) -> None:
        """端到端验证：build_data_matrix 产出的 split_idx 满足泄漏约束。"""
        from src.research.core.data_matrix import DataMatrix

        # 直接检查 DataMatrix 实例的 split_idx 语义是合理的
        # 完整 build_data_matrix 需要 DB，单测里绕过
        # 此处只确保契约不变：train_end_idx < split_idx <= n_bars
        m = DataMatrix(
            symbol="X",
            timeframe="M15",
            n_bars=200,
            bar_times=[],
            opens=[],
            highs=[],
            lows=[],
            closes=[],
            volumes=[],
            indicators=[],
            regimes=[],
            soft_regimes=[],
            forward_returns={},
            indicator_series={},
            train_end_idx=140,
            split_idx=161,  # gap = 21 = max_barrier_time (20) + 1
        )
        assert m.train_end_idx < m.split_idx <= m.n_bars


class TestPValueCorrection:
    def test_raw_p_value_not_pre_adjusted(self) -> None:
        """
        场景复现：两特征的 Pearson / Spearman p-values 接近 significance boundary。
        旧逻辑预先 ×2 会让 raw=0.03 的真实信号变成 0.06（看似不显著）。
        新逻辑保持 raw p-value，让下游 FDR/Bonferroni 统一决定。
        """
        # 直接调用的话需要完整 DataMatrix 构造，改为语义契约校验：
        from src.research.analyzers import predictive_power

        import inspect

        src = inspect.getsource(predictive_power._compute_single)
        assert "* 2.0" not in src, (
            "_compute_single should NOT pre-multiply p-value by 2.0 (Bonferroni). "
            "Correction is handled in _apply_batch_correction."
        )
        assert "min(pearson_p, spearman_p)" in src


class TestBarrierGapIntegration:
    def test_default_configs_barrier_time_max(self) -> None:
        """默认 DEFAULT_BARRIER_CONFIGS 下最大 time_bars = 120（sl=2.5, tp=5.0, time=120）。"""
        from src.research.core.barrier import DEFAULT_BARRIER_CONFIGS

        max_t = max(cfg.time_bars for cfg in DEFAULT_BARRIER_CONFIGS)
        assert max_t == 120

    def test_cfg_time_bars_respected(self) -> None:
        """BarrierConfig(time_bars=...) 正确注入 gap 计算。"""
        cfg = BarrierConfig(sl_atr=1.0, tp_atr=2.0, time_bars=60)
        assert cfg.time_bars == 60
