"""过拟合防护工具 — 时序 CV 分割 + 多重比较校正 + 统计效力。

数据挖掘极易过拟合，本模块提供 6 层防护：
  L1: Train/Test 分割（DataMatrix 级别）
  L2: 多重比较校正（BH-FDR / Bonferroni）— 输入应优先使用排列 p-value
  L3: 时序交叉验证（expanding + sliding 两种模式）
  L4: 最小样本量门槛
  L5: 效应量门槛
  L6: 统计效力评估（报告 minimum detectable IC）

纯函数设计，不依赖运行时组件。
统计原语（ACF、block_shuffle 等）来自 statistics.py。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class TimeSeriesFold:
    """时序交叉验证的一个 fold。"""

    fold_index: int
    train_start: int
    train_end: int  # exclusive
    test_start: int
    test_end: int  # exclusive


def time_series_cv_splits(
    n_samples: int,
    n_folds: int = 5,
    min_train_size: int = 50,
    *,
    mode: str = "expanding",
) -> List[TimeSeriesFold]:
    """生成时序交叉验证的分割。

    两种模式：
      - expanding: 训练窗口从 0 开始逐步扩大（默认，偏向利用全部历史）
      - sliding: 训练窗口固定大小滑动（适合非平稳市场，避免远古数据稀释近期模式）

    两种模式均保证训练数据始终在测试数据之前，避免未来信息泄露。

    Args:
        n_samples: 总样本数
        n_folds: 折数
        min_train_size: 最小训练集大小
        mode: "expanding" | "sliding"

    Returns:
        TimeSeriesFold 列表
    """
    if n_samples < min_train_size + n_folds:
        return []

    fold_size = (n_samples - min_train_size) // n_folds
    if fold_size < 1:
        return []

    folds: List[TimeSeriesFold] = []
    for i in range(n_folds):
        train_end = min_train_size + i * fold_size
        test_start = train_end
        test_end = min(test_start + fold_size, n_samples)
        if test_start >= test_end:
            break

        if mode == "sliding":
            # 固定窗口大小：train_start 随 fold 推进
            train_start = max(0, train_end - min_train_size)
        else:
            # expanding：始终从头开始
            train_start = 0

        folds.append(
            TimeSeriesFold(
                fold_index=i,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
            )
        )
    return folds


def bonferroni_correction(p_value: float, n_tests: int) -> float:
    """Bonferroni 多重比较校正。

    最保守的校正方法：adjusted_p = min(p × n_tests, 1.0)。
    """
    if n_tests < 1:
        return p_value
    return min(p_value * n_tests, 1.0)


def benjamini_hochberg_fdr(
    p_values: List[float],
    alpha: float = 0.05,
    *,
    n_total_tests: Optional[int] = None,
) -> List[Tuple[int, float, bool]]:
    """Benjamini-Hochberg False Discovery Rate 校正。

    控制"所有声称显著的发现中，假阳性比例 ≤ α"。
    比 Bonferroni 更合理，在大量检验时不会压掉所有真发现。

    Args:
        p_values: 原始 p-value 列表（每个检验一个）
        alpha: 目标 FDR 水平（默认 0.05）
        n_total_tests: 实际尝试的总检验数（含被 min_samples 过滤的）。
                       当部分检验因样本量不足被丢弃时，m 应反映尝试总数，
                       否则 FDR 控制过于宽松。None = 使用 len(p_values)。

    Returns:
        [(original_index, adjusted_p_value, is_significant)] 按原始索引排序
    """
    m_present = len(p_values)
    if m_present == 0:
        return []

    # m = 实际尝试过的检验总数（包括被过滤掉的）
    m = n_total_tests if n_total_tests is not None else m_present
    m = max(m, m_present)  # 安全下限

    # 按 p-value 升序排列，保留原始索引
    indexed = sorted(enumerate(p_values), key=lambda x: x[1])

    # 计算 adjusted p-value（使用 m 而非 m_present）
    adjusted: List[Tuple[int, float]] = []
    for rank_0based, (orig_idx, p) in enumerate(indexed):
        rank = rank_0based + 1  # 1-based
        adj_p = min(p * m / rank, 1.0)
        adjusted.append((orig_idx, adj_p))

    # 从右向左强制单调性
    for i in range(len(adjusted) - 2, -1, -1):
        adj_p_cur = adjusted[i][1]
        adj_p_next = adjusted[i + 1][1]
        if adj_p_cur > adj_p_next:
            adjusted[i] = (adjusted[i][0], adj_p_next)

    # 构建结果，按原始索引排序
    results = [(orig_idx, adj_p, adj_p <= alpha) for orig_idx, adj_p in adjusted]
    results.sort(key=lambda x: x[0])
    return results


def check_significance(
    p_value: float,
    n_samples: int,
    correlation: float,
    hit_rate_deviation: float,
    *,
    significance_level: float = 0.05,
    min_samples: int = 30,
    min_correlation: float = 0.05,
    min_hit_rate_deviation: float = 0.03,
    n_tests: int = 1,
    apply_bonferroni: bool = True,
) -> Tuple[bool, str]:
    """综合检验发现是否有统计意义（L2+L4+L5）。

    Returns:
        (is_significant, confidence_level)
        confidence_level: "high" | "medium" | "low" | "insufficient"
    """
    # L4: 样本量门槛
    if n_samples < min_samples:
        return False, "insufficient"

    # L5: 效应量门槛
    if (
        abs(correlation) < min_correlation
        and abs(hit_rate_deviation) < min_hit_rate_deviation
    ):
        return False, "low"

    # L2: 多重比较校正
    adjusted_p = (
        bonferroni_correction(p_value, n_tests) if apply_bonferroni else p_value
    )

    if adjusted_p >= significance_level:
        return False, "low"

    # 分级置信度
    if n_samples >= 100 and adjusted_p < 0.01 and abs(correlation) > 0.10:
        return True, "high"
    elif n_samples >= 50:
        return True, "medium"
    else:
        return True, "low"


def compute_cv_consistency(
    fold_optimal_values: List[Optional[float]],
    tolerance_pct: float = 0.20,
) -> float:
    """计算交叉验证一致性：最优值在各 fold 间的稳定程度。

    Args:
        fold_optimal_values: 每个 fold 的最优阈值（None = 无有效结果）
        tolerance_pct: 允许的偏差百分比（相对于均值的比例）

    Returns:
        一致性比例 (0.0 ~ 1.0)
    """
    valid = [v for v in fold_optimal_values if v is not None]
    if len(valid) < 2:
        return 0.0

    mean_val = sum(valid) / len(valid)
    if abs(mean_val) < 1e-12:
        # 均值接近零时用绝对值范围
        val_range = max(valid) - min(valid)
        if val_range < 1e-12:
            return 1.0
        consistent = sum(1 for v in valid if abs(v) < val_range * tolerance_pct)
    else:
        tolerance = abs(mean_val) * tolerance_pct
        consistent = sum(1 for v in valid if abs(v - mean_val) <= tolerance)

    return consistent / len(valid)
