"""排列检验并行化契约测试。"""

from __future__ import annotations

import random

import pytest
from scipy import stats as scipy_stats

from src.research.core.permutation import (
    PermutationEngine,
    PermutationTestResult,
    PermutationTestSpec,
    _run_serial_chunk,
    run_permutation_test,
)
from src.research.core.statistics import block_shuffle


def _make_spec(
    n: int = 100, n_perm: int = 50, correlated: bool = True
) -> PermutationTestSpec:
    """生成 spec：correlated=True 时 ind 与 fwd 高度正相关。"""
    if correlated:
        ind = tuple(float(i) for i in range(n))
        fwd = tuple(float(i) * 1.05 for i in range(n))  # 线性正相关
    else:
        # 反相关随机序列
        import random

        rng = random.Random(0)
        vals = [rng.random() for _ in range(n)]
        ind = tuple(vals)
        fwd = tuple(rng.random() for _ in range(n))  # 独立
    return PermutationTestSpec(
        ind_vals=ind,
        fwd_vals=fwd,
        observed_ic=0.9 if correlated else 0.05,
        n_permutations=n_perm,
        block_size=5,
        seed=42,
    )


class TestContract:
    def test_mismatched_lengths_raise(self) -> None:
        with pytest.raises(ValueError):
            PermutationTestSpec(
                ind_vals=(1.0, 2.0),
                fwd_vals=(1.0,),
                observed_ic=0.5,
                n_permutations=10,
                block_size=2,
                seed=0,
            )

    def test_zero_permutations_raise(self) -> None:
        with pytest.raises(ValueError):
            PermutationTestSpec(
                ind_vals=(1.0, 2.0),
                fwd_vals=(1.0, 2.0),
                observed_ic=0.5,
                n_permutations=0,
                block_size=1,
                seed=0,
            )

    def test_zero_block_size_raise(self) -> None:
        with pytest.raises(ValueError):
            PermutationTestSpec(
                ind_vals=(1.0, 2.0),
                fwd_vals=(1.0, 2.0),
                observed_ic=0.5,
                n_permutations=10,
                block_size=0,
                seed=0,
            )

    def test_zero_workers_raise(self) -> None:
        spec = _make_spec()
        with pytest.raises(ValueError):
            run_permutation_test(spec, workers=0)


class TestSerial:
    def test_strong_signal_low_pvalue(self) -> None:
        spec = _make_spec(correlated=True, n_perm=100)
        result = run_permutation_test(spec, workers=1)
        assert isinstance(result, PermutationTestResult)
        # 观测 IC=0.9 几乎不可能被随机排列达到 → p-value 应极小
        assert result.p_value < 0.1
        assert result.effective_permutations <= 100

    def test_weak_signal_high_pvalue(self) -> None:
        spec = _make_spec(correlated=False, n_perm=100)
        result = run_permutation_test(spec, workers=1)
        # 观测 IC=0.05 极易被随机排列达到 → p-value 应较高
        assert result.p_value > 0.3


class TestPreRankEquivalence:
    """Phase R.2 后续优化：pre-rank + numpy pearson vs 原 scipy_stats.spearmanr。

    新实现与"每次 permutation 调 spearmanr"在数值上必须等价（容差 1e-9）。
    """

    @staticmethod
    def _legacy_chunk(
        ind_vals: list,
        fwd_vals: list,
        abs_observed_ic: float,
        n: int,
        block_size: int,
        seed: int,
    ) -> tuple:
        """重构前的逻辑：每次 permutation 调 scipy_stats.spearmanr。"""
        rng = random.Random(seed)
        count_ge = 0
        effective = 0
        for _ in range(n):
            shuffled = block_shuffle(fwd_vals, block_size, rng)
            try:
                rho, _ = scipy_stats.spearmanr(ind_vals, shuffled)
            except Exception:
                continue
            effective += 1
            if abs(rho) >= abs_observed_ic:
                count_ge += 1
        return count_ge, effective

    @pytest.mark.parametrize("seed", [0, 7, 42, 999])
    @pytest.mark.parametrize("block_size", [3, 5, 10])
    def test_equivalent_to_legacy_random_data(self, seed, block_size):
        """随机数据下 (count_ge, effective) 必须完全一致。"""
        rng = random.Random(seed * 31 + 17)
        n_samples = 80
        ind = [rng.gauss(0, 1) for _ in range(n_samples)]
        fwd = [rng.gauss(0, 1) for _ in range(n_samples)]
        # 设一个中等 observed_ic，确保 pass/fail 分布有变化
        legacy = self._legacy_chunk(
            ind, fwd, abs_observed_ic=0.05, n=50, block_size=block_size, seed=seed
        )
        new = _run_serial_chunk(
            ind_vals=ind,
            fwd_vals=fwd,
            abs_observed_ic=0.05,
            n=50,
            block_size=block_size,
            seed=seed,
        )
        assert legacy == new, f"legacy {legacy} != new {new}"

    def test_equivalent_to_legacy_correlated(self):
        """高相关数据：两种实现都应给出极低 count_ge。"""
        n_samples = 100
        ind = [float(i) for i in range(n_samples)]
        fwd = [float(i) * 1.05 + (i % 7) for i in range(n_samples)]
        legacy = self._legacy_chunk(
            ind, fwd, abs_observed_ic=0.85, n=200, block_size=5, seed=42
        )
        new = _run_serial_chunk(
            ind_vals=ind,
            fwd_vals=fwd,
            abs_observed_ic=0.85,
            n=200,
            block_size=5,
            seed=42,
        )
        assert legacy == new

    def test_constant_input_returns_empty(self):
        """ind 或 fwd 全相同 → std=0 → 全部 ineffective。"""
        n_samples = 50
        const = [3.14] * n_samples
        non_const = [float(i) for i in range(n_samples)]
        out = _run_serial_chunk(
            ind_vals=const,
            fwd_vals=non_const,
            abs_observed_ic=0.5,
            n=10,
            block_size=3,
            seed=0,
        )
        assert out == (0, 0)

    def test_e2e_p_value_close_to_legacy(self):
        """对外 p_value 接口在 noise 数据上与 legacy 一致（容差 0）。"""
        rng = random.Random(123)
        n_samples = 60
        ind = tuple(rng.gauss(0, 1) for _ in range(n_samples))
        fwd = tuple(rng.gauss(0, 1) for _ in range(n_samples))
        spec = PermutationTestSpec(
            ind_vals=ind,
            fwd_vals=fwd,
            observed_ic=0.1,
            n_permutations=80,
            block_size=4,
            seed=2026,
        )
        result = run_permutation_test(spec, workers=1)
        # 与 legacy 直接比对（同 seed 同 block 同 n_permutations）
        legacy_count, legacy_eff = self._legacy_chunk(
            list(ind),
            list(fwd),
            abs_observed_ic=abs(0.1),
            n=80,
            block_size=4,
            seed=2026,
        )
        legacy_p = legacy_count / max(legacy_eff, 1)
        assert abs(result.p_value - legacy_p) < 1e-12
        assert result.effective_permutations == legacy_eff


# ── PermutationEngine 通用接口（P0 收编 threshold + rule_mining） ──────────


class TestPermutationEngine:
    """通用 PermutationEngine：覆盖 collect_null_distribution / p_value_*。"""

    def test_validation_rejects_zero_block(self):
        with pytest.raises(ValueError):
            PermutationEngine(block_size=0, n_permutations=10, seed=0)

    def test_validation_rejects_zero_perm(self):
        with pytest.raises(ValueError):
            PermutationEngine(block_size=3, n_permutations=0, seed=0)

    def test_collect_null_distribution_length(self):
        """每次 statistic 返回 float → 收集长度应 == n_permutations。"""
        engine = PermutationEngine(block_size=3, n_permutations=20, seed=42)
        nulls = engine.collect_null_distribution(
            shuffle_target=[1.0, 2.0, 3.0, 4.0, 5.0] * 4,
            compute_statistic=lambda sh: float(sum(sh)),  # sum 不变（重排）
        )
        assert len(nulls) == 20
        assert all(s == 60.0 for s in nulls)  # sum 始终 60

    def test_collect_null_distribution_skips_none(self):
        """statistic 返回 None → 跳过，effective < n_permutations。"""
        call_count = {"n": 0}

        def stat(sh):
            call_count["n"] += 1
            return None if call_count["n"] % 2 == 0 else 1.0

        engine = PermutationEngine(block_size=2, n_permutations=10, seed=0)
        nulls = engine.collect_null_distribution([1.0, 2.0, 3.0, 4.0], stat)
        assert len(nulls) == 5  # 一半 None
        assert call_count["n"] == 10

    def test_collect_null_distribution_skips_exception(self):
        """statistic 抛异常 → 跳过。"""

        def stat(sh):
            raise ValueError("boom")

        engine = PermutationEngine(block_size=2, n_permutations=10, seed=0)
        nulls = engine.collect_null_distribution([1.0, 2.0, 3.0, 4.0], stat)
        assert nulls == []

    def test_p_value_one_sided_basic(self):
        """observed 比所有 null 都大 → p ≈ 0；都小 → p ≈ 1。"""
        engine = PermutationEngine(block_size=2, n_permutations=20, seed=7)

        # null 全 0.5，observed 0.9
        p_low, eff_low = engine.p_value_one_sided(
            shuffle_target=[1.0, 2.0, 3.0, 4.0],
            compute_statistic=lambda sh: 0.5,
            observed=0.9,
        )
        assert p_low == 0.0
        assert eff_low == 20

        # null 全 0.5，observed 0.1
        p_high, _ = engine.p_value_one_sided(
            shuffle_target=[1.0, 2.0, 3.0, 4.0],
            compute_statistic=lambda sh: 0.5,
            observed=0.1,
        )
        assert p_high == 1.0  # 所有 null >= observed

    def test_p_value_one_sided_empty_returns_conservative(self):
        engine = PermutationEngine(block_size=2, n_permutations=10, seed=0)
        p, eff = engine.p_value_one_sided(
            shuffle_target=[1.0, 2.0, 3.0],
            compute_statistic=lambda sh: None,
            observed=0.5,
        )
        assert p == 1.0
        assert eff == 0

    def test_p_value_abs_uses_absolute_value(self):
        """null 是 +0.5/-0.5 混合，observed=0.3：abs 比较都 ≥ 0.3 → p=1.0。"""
        engine = PermutationEngine(block_size=2, n_permutations=10, seed=11)
        # 用 perm_index 控制返回正负
        toggle = {"flip": True}

        def stat(sh):
            toggle["flip"] = not toggle["flip"]
            return 0.5 if toggle["flip"] else -0.5

        p, eff = engine.p_value_abs(
            shuffle_target=[1.0, 2.0, 3.0, 4.0],
            compute_statistic=stat,
            abs_observed=0.3,
        )
        assert p == 1.0
        assert eff == 10

    def test_seed_reproducibility(self):
        """同 seed → 同 shuffle 序列 → 同结果。"""
        target = [float(i) for i in range(20)]

        captured_a, captured_b = [], []

        def make_stat(captured):
            def stat(sh):
                captured.append(tuple(sh))
                return 1.0

            return stat

        eng = PermutationEngine(block_size=3, n_permutations=15, seed=2026)
        eng.collect_null_distribution(target, make_stat(captured_a))
        eng.collect_null_distribution(target, make_stat(captured_b))
        assert captured_a == captured_b  # 完全可复现
