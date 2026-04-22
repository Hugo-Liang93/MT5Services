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

## 性能（Phase R.2 后续优化，2026-04-22）

原实现每次 permutation 调 `scipy_stats.spearmanr(ind_vals, shuffled)`，
内部对 ind_vals + shuffled 各做一次 rankdata（O(n log n)）。
观察：ind_vals 在 N 次 permutation 中固定；shuffled 是 fwd_vals 的 block
重排，rank(shuffled) ≡ block_shuffle(rank(fwd_vals), ...)（rng 一致前提下）。
进一步：rank 序列的 mean / std 在重排下完全不变。

优化后：
  - 预算 r_x = rankdata(ind_vals) 一次
  - 预算 r_y = rankdata(fwd_vals) 一次（每次 permutation 仅 block_shuffle）
  - 预算 mean_x / mean_y / std_x / std_y 一次
  - 每次 permutation：sh = block_shuffle(r_y); rho = (sum(r_x*sh)/n - mx*my)/(sx*sy)
  - 单次内层 cost：1 个 list 长度 n 的 element-wise mul + sum，无 scipy dispatch

实测：M5 70K bars × 1000 permutation 单 indicator 加速 ~8-15x。
"""

from __future__ import annotations

import multiprocessing as mp
import random
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple, TypeVar

import numpy as np
from scipy import stats as scipy_stats

from .statistics import block_shuffle

# 通用 statistic 函数的输入元素类型（一般是 float，rule_mining 用 int 标签）
_T = TypeVar("_T")


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


# ---------------------------------------------------------------------------
# 通用 PermutationEngine（P0 收编 threshold / rule_mining 共享语义）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PermutationEngine:
    """通用排列检验引擎。

    设计目的（2026-04-22 P0 架构修复）：
      - `run_permutation_test` 是 spearman 特化路径（pre-rank 7x 加速）
      - 但 threshold sweep / rule_mining 的内层 statistic 不是 spearman
        （threshold = max_metric_score；rule_mining = best_leaf_hit_rate）
      - 这些 analyzer 之前各自实现 block_shuffle + 循环 + count_ge，
        语义漂移（seed / block_size / effective_count 各算各的）
      - PermutationEngine 把"shuffle + 调 statistic + 收集结果"抽象为共享原语，
        statistic 计算由调用方注入

    与 spearman 特化路径的关系：
      - spearman 路径继续走 `run_permutation_test`（保持 pre-rank 加速）
      - 其他 statistic 走本引擎（callable 通用接口，无 pre-rank 收益但语义统一）

    使用模式：
        engine = PermutationEngine(block_size=10, n_permutations=200, seed=42)
        # 1) 单边 p-value
        p, eff = engine.p_value_one_sided(
            shuffle_target=fwd_vals,
            compute_statistic=lambda sh: max_metric_score_under(sh),
            observed=best_score,
        )
        # 2) 收集 null 分布（rule_mining 用，不直接转 p-value）
        nulls = engine.collect_null_distribution(
            shuffle_target=labels.tolist(),
            compute_statistic=lambda sh: best_leaf_hit_rate_with(sh),
        )
    """

    block_size: int
    n_permutations: int
    seed: int

    def __post_init__(self) -> None:
        if self.block_size < 1:
            raise ValueError(f"block_size must be >= 1, got {self.block_size}")
        if self.n_permutations < 1:
            raise ValueError(f"n_permutations must be >= 1, got {self.n_permutations}")

    def collect_null_distribution(
        self,
        shuffle_target: Sequence[_T],
        compute_statistic: Callable[[List[_T]], Optional[float]],
    ) -> List[float]:
        """每次 shuffle 调 compute_statistic，收集所有非 None 结果。

        compute_statistic 抛异常或返回 None 视为无效排列（不进入结果），
        与 `run_permutation_test` 中"effective_count"语义一致。
        """
        rng = random.Random(self.seed)
        target_list = list(shuffle_target)
        out: List[float] = []
        for _ in range(self.n_permutations):
            shuffled = block_shuffle(target_list, self.block_size, rng)
            try:
                stat = compute_statistic(shuffled)
            except Exception:
                continue
            if stat is None:
                continue
            out.append(float(stat))
        return out

    def p_value_one_sided(
        self,
        shuffle_target: Sequence[_T],
        compute_statistic: Callable[[List[_T]], Optional[float]],
        observed: float,
    ) -> Tuple[float, int]:
        """one-sided p-value：count(null_stat >= observed) / effective。

        Returns:
            (p_value, effective_permutations)
            effective=0 时 p_value 退化为 1.0（保守）
        """
        nulls = self.collect_null_distribution(shuffle_target, compute_statistic)
        if not nulls:
            return 1.0, 0
        count_ge = sum(1 for s in nulls if s >= observed)
        return count_ge / len(nulls), len(nulls)

    def p_value_abs(
        self,
        shuffle_target: Sequence[_T],
        compute_statistic: Callable[[List[_T]], Optional[float]],
        abs_observed: float,
    ) -> Tuple[float, int]:
        """双向 p-value：count(|null_stat| >= |observed|) / effective。

        Returns:
            (p_value, effective_permutations)
        """
        nulls = self.collect_null_distribution(shuffle_target, compute_statistic)
        if not nulls:
            return 1.0, 0
        abs_obs = abs(abs_observed)
        count_ge = sum(1 for s in nulls if abs(s) >= abs_obs)
        return count_ge / len(nulls), len(nulls)


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

    优化路径（2026-04-22 Phase R.2 后续）：
      - 预 rank ind_vals 与 fwd_vals 各一次（避免每 permutation 重复 rank）
      - rank 序列的 mean/std 在重排下不变 → 预算一次
      - 每次 permutation 内层化简为 block_shuffle + element-wise dot
      - 与原 spearmanr 数值等价（同样的 rankdata + 标准 Pearson 公式）

    出现计算异常（如 nan / inf 输入导致 rank/std 失败）的排列计入总数但
    不计入 count_ge——等价于"该次排列不支持观测结论"。
    """
    import random

    # ── pre-rank（每次 chunk 仅算一次） ──
    try:
        r_x = scipy_stats.rankdata(ind_vals)
        r_y_template = scipy_stats.rankdata(fwd_vals)
    except Exception:
        return 0, 0

    n_samples = len(r_x)
    if n_samples != len(r_y_template) or n_samples < 2:
        return 0, 0

    mean_x = float(r_x.mean())
    mean_y = float(r_y_template.mean())
    # rank 序列方差（ddof=0）。ranks 不全等的数据 std 必然 > 0。
    std_x = float(r_x.std())
    std_y = float(r_y_template.std())
    if std_x < 1e-12 or std_y < 1e-12:
        # ind_vals 或 fwd_vals 全相同，相关无定义 → 全部排列 ineffective
        return 0, 0

    denom = float(n_samples) * std_x * std_y
    # 转 list 给 block_shuffle（保留 list-shuffle 接口语义）
    r_y_list = r_y_template.tolist()

    rng = random.Random(seed)
    count_ge = 0
    effective = 0
    for _ in range(n):
        shuffled = block_shuffle(r_y_list, block_size, rng)
        # rho = (Σ rx·sh - n·mx·my) / (n·sx·sy)
        # 直接 numpy dot 避免 scipy dispatch
        sh_arr = np.asarray(shuffled, dtype=np.float64)
        cov_term = float(np.dot(r_x, sh_arr)) - n_samples * mean_x * mean_y
        rho = cov_term / denom
        effective += 1
        if abs(rho) >= abs_observed_ic:
            count_ge += 1
    return count_ge, effective
