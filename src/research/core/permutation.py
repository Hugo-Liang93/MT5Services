"""排列检验并行化契约。

## 职责

将原本在 `analyzers/predictive_power.py` 内部的排列检验循环抽取为契约化模块。
两个核心理由：
  1. **共享**：`rule_mining.py`、`threshold.py` 也需要类似排列检验，避免重复实现
  2. **并行化**：排列之间独立，直接用 ProcessPool 扩展到 CPU 核数

## 契约

- `PermutationTestSpec` (frozen dataclass)：排列检验的完整输入
- `PermutationTestResult` (frozen dataclass)：输出（p-value + 实际执行次数）
- `run_permutation_test(spec, *, workers=1)` → `PermutationTestResult`：纯函数入口

## 设计纪律

- 无默认值兜底：spec 字段必填。种子 / block_size 由调用方计算后传入
- 无 fallback：并行化失败不静默降级为串行，直接抛异常
- workers=1 与 workers>1 走同一语义路径（确保可复现性）
"""

from __future__ import annotations

import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Tuple

from scipy import stats as scipy_stats

from .statistics import block_shuffle


@dataclass(frozen=True)
class PermutationTestSpec:
    """Block-permutation Spearman IC 检验的完整输入。

    Attributes:
        ind_vals: 特征值序列
        fwd_vals: 前瞻收益序列（与 ind_vals 等长）
        observed_ic: 已观测的 Spearman IC
        n_permutations: 排列次数（≥ 1）
        block_size: block_shuffle 块长度（>= 1）。应由 auto_block_size 决定
        seed: 根种子。并行路径下每个 worker 以 seed+i 派生独立子种子
    """

    ind_vals: Tuple[float, ...]
    fwd_vals: Tuple[float, ...]
    observed_ic: float
    n_permutations: int
    block_size: int
    seed: int

    def __post_init__(self) -> None:
        if len(self.ind_vals) != len(self.fwd_vals):
            raise ValueError(
                f"ind_vals/fwd_vals length mismatch: "
                f"{len(self.ind_vals)} vs {len(self.fwd_vals)}"
            )
        if self.n_permutations < 1:
            raise ValueError(f"n_permutations must be >= 1, got {self.n_permutations}")
        if self.block_size < 1:
            raise ValueError(f"block_size must be >= 1, got {self.block_size}")


@dataclass(frozen=True)
class PermutationTestResult:
    p_value: float
    effective_permutations: int  # 实际成功执行的次数（<=n_permutations）


def run_permutation_test(
    spec: PermutationTestSpec, *, workers: int = 1
) -> PermutationTestResult:
    """执行排列检验。

    Args:
        spec: 输入契约
        workers: 并行进程数。1 = 串行；>1 = multiprocessing

    Returns:
        PermutationTestResult(p_value, effective_permutations)

    Raises:
        ValueError: workers < 1
    """
    if workers < 1:
        raise ValueError(f"workers must be >= 1, got {workers}")

    if workers == 1:
        count_ge, effective = _run_serial_chunk(
            ind_vals=list(spec.ind_vals),
            fwd_vals=list(spec.fwd_vals),
            abs_observed_ic=abs(spec.observed_ic),
            n=spec.n_permutations,
            block_size=spec.block_size,
            seed=spec.seed,
        )
        return PermutationTestResult(
            p_value=count_ge / max(effective, 1),
            effective_permutations=effective,
        )

    # 并行：将 n_permutations 均分给 workers，每 worker 用独立子种子
    chunk_sizes = _split_chunks(spec.n_permutations, workers)
    # 每 chunk 的 seed 从 spec.seed 派生（保证可复现 + 避免 worker 间重复序列）
    tasks = [
        (
            list(spec.ind_vals),
            list(spec.fwd_vals),
            abs(spec.observed_ic),
            size,
            spec.block_size,
            spec.seed + i * 10_007,
        )
        for i, size in enumerate(chunk_sizes)
    ]

    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
        results = list(pool.map(_run_serial_chunk_packed, tasks))

    total_ge = sum(r[0] for r in results)
    total_eff = sum(r[1] for r in results)
    return PermutationTestResult(
        p_value=total_ge / max(total_eff, 1),
        effective_permutations=total_eff,
    )


# ── 内部 worker（模块级以便 multiprocessing pickle）─────────────


def _split_chunks(total: int, workers: int) -> Tuple[int, ...]:
    """将 total 均分 workers 份，余数摊到前几份。"""
    if workers <= 0:
        raise ValueError(f"workers must be > 0, got {workers}")
    base, rem = divmod(total, workers)
    return tuple(base + (1 if i < rem else 0) for i in range(workers))


def _run_serial_chunk_packed(
    args: Tuple[list, list, float, int, int, int],
) -> Tuple[int, int]:
    """ProcessPool 友好的参数解包包装器。"""
    ind_vals, fwd_vals, abs_obs, n, block, seed = args
    return _run_serial_chunk(
        ind_vals=ind_vals,
        fwd_vals=fwd_vals,
        abs_observed_ic=abs_obs,
        n=n,
        block_size=block,
        seed=seed,
    )


def _run_serial_chunk(
    *,
    ind_vals: list,
    fwd_vals: list,
    abs_observed_ic: float,
    n: int,
    block_size: int,
    seed: int,
) -> Tuple[int, int]:
    """串行执行一段排列：返回 (count_ge, effective_count)。

    出现计算异常（如 nan / inf 输入导致 spearmanr 失败）的排列计入总数但
    不计入 count_ge——等价于"该次排列不支持观测结论"。
    """
    import random

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
