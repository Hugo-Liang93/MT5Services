"""排列检验并行化契约测试。"""

from __future__ import annotations

import pytest

from src.research.core.permutation import (
    PermutationTestResult,
    PermutationTestSpec,
    run_permutation_test,
)


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
