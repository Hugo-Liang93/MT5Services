"""纯统计工具模块 — 为 research 各分析器提供可复用的统计原语。

职责边界：通用统计计算，不涉及交易逻辑或 DataMatrix 结构。
所有函数均为纯函数，无副作用。

提供的能力：
  - 自适应 block size（基于自相关衰减）
  - 有效样本量校正（重叠窗口 / 自相关序列）
  - 最小可检测效应量（统计效力分析）
  - Newey-West 自相关一致标准误
  - Binomial 精确检验
  - Block permutation shuffle
"""

from __future__ import annotations

import math
import random
from typing import List, Optional, Tuple


# ── 自相关分析 ──────────────────────────────────────────────────


def estimate_autocorrelation(values: List[float], max_lag: int = 50) -> List[float]:
    """计算序列自相关函数 ACF(0..max_lag)。

    Args:
        values: 时序数据
        max_lag: 最大滞后阶数

    Returns:
        acf[k] = autocorrelation at lag k, acf[0] = 1.0
    """
    n = len(values)
    if n < 4:
        return [1.0]

    max_lag = min(max_lag, n // 4)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    if var < 1e-15:
        return [1.0] + [0.0] * max_lag

    acf: List[float] = [1.0]
    for k in range(1, max_lag + 1):
        cov_k = sum((values[i] - mean) * (values[i + k] - mean) for i in range(n - k))
        acf.append(cov_k / (var * n))
    return acf


def auto_block_size(values: List[float], max_lag: int = 50) -> int:
    """基于自相关衰减长度自动选择 block permutation 的 block size。

    算法：找到 ACF 首次降至 |acf| < 0.05 的 lag，
    取该 lag 作为 block size（最小 5，最大 max_lag）。

    这比硬编码 block_size=10 更能适应不同 TF 的自相关结构：
      - M5 数据自相关衰减快 → 小 block
      - H1/D1 数据自相关持续久 → 大 block
    """
    n = len(values)
    if n < 20:
        return 5

    acf = estimate_autocorrelation(values, max_lag=max_lag)
    threshold = 0.05

    for lag in range(1, len(acf)):
        if abs(acf[lag]) < threshold:
            return max(5, lag)

    # ACF 未衰减到阈值以下 — 取 max_lag
    return max(5, min(max_lag, n // 10))


# ── 有效样本量 ──────────────────────────────────────────────────


def effective_sample_size(n: int, acf_values: List[float]) -> float:
    """计算考虑自相关后的有效样本量。

    Kish 公式的推广：n_eff = n / (1 + 2·Σ acf(k))
    当序列高度自相关时，n_eff << n，反映真实的独立信息量。

    Args:
        n: 原始样本数
        acf_values: ACF(0..K)，acf[0] 应为 1.0

    Returns:
        有效样本量（≥ 1）
    """
    if n <= 1 or len(acf_values) < 2:
        return float(n)

    # 累加正自相关部分（负自相关开始后停止）
    sum_acf = 0.0
    for k in range(1, len(acf_values)):
        if acf_values[k] < 0:
            break
        sum_acf += acf_values[k]

    denominator = 1.0 + 2.0 * sum_acf
    return max(1.0, n / denominator)


def effective_n_for_overlapping_windows(
    n_windows: int, window_size: int, step_size: int
) -> float:
    """计算重叠窗口的有效独立窗口数。

    当 step < window_size 时，相邻窗口共享大量数据，
    统计量（如 rolling IC）高度自相关，标准误被低估。

    近似公式：n_eff ≈ n_windows × (step / window_size)

    Args:
        n_windows: 实际窗口数
        window_size: 窗口大小
        step_size: 步长

    Returns:
        有效独立窗口数（≥ 1）
    """
    if window_size <= 0 or step_size <= 0:
        return max(1.0, float(n_windows))

    overlap_ratio = min(1.0, step_size / window_size)
    return max(1.0, n_windows * overlap_ratio)


# ── 统计效力分析 ──────────────────────────────────────────────────


def minimum_detectable_ic(
    n_samples: int,
    alpha: float = 0.05,
    power: float = 0.80,
) -> float:
    """给定样本量和显著性水平，计算可检测的最小 IC（效应量）。

    基于 Fisher z 变换的相关系数检验：
      z = arctanh(r) × sqrt(n - 3) ~ N(0, 1) under H0
      需要 |z| > z_alpha + z_beta 才能检测到

    现实意义（XAUUSD 经验值）：
      n=30  → min_ic ≈ 0.36（仅能检测极强信号）
      n=100 → min_ic ≈ 0.20
      n=400 → min_ic ≈ 0.10
      n=1600 → min_ic ≈ 0.05（可检测弱信号）

    Args:
        n_samples: 样本数
        alpha: 显著性水平（双尾）
        power: 统计效力（1 - β）

    Returns:
        最小可检测的 |IC| 值
    """
    if n_samples <= 3:
        return 1.0

    z_alpha = _norm_ppf(1.0 - alpha / 2.0)
    z_beta = _norm_ppf(power)
    z_total = z_alpha + z_beta

    # Fisher z: z = arctanh(r) × sqrt(n-3)
    # 解出 r: arctanh(r) = z_total / sqrt(n-3) → r = tanh(z_total / sqrt(n-3))
    fisher_z = z_total / math.sqrt(n_samples - 3)
    ic = math.tanh(fisher_z)
    return min(ic, 1.0)


def required_sample_size(
    target_ic: float,
    alpha: float = 0.05,
    power: float = 0.80,
) -> int:
    """给定目标 IC，计算所需最小样本量。

    Args:
        target_ic: 目标 |IC| 值
        alpha: 显著性水平（双尾）
        power: 统计效力

    Returns:
        所需样本数
    """
    if abs(target_ic) < 1e-6 or abs(target_ic) >= 1.0:
        return 10000  # 不可能或不需要

    z_alpha = _norm_ppf(1.0 - alpha / 2.0)
    z_beta = _norm_ppf(power)
    z_total = z_alpha + z_beta

    # n = (z_total / arctanh(r))^2 + 3
    fisher_z = math.atanh(abs(target_ic))
    n = (z_total / fisher_z) ** 2 + 3
    return max(4, math.ceil(n))


# ── Binomial 精确检验 ──────────────────────────────────────────


def binomial_test_p(k: int, n: int, p0: float = 0.5) -> float:
    """双尾 binomial 精确检验的 p-value。

    检验 H0: hit_rate = p0（默认 50%）。
    用于规则挖掘中验证 test set hit_rate 是否显著优于随机。

    Args:
        k: 成功次数（命中数）
        n: 总次数
        p0: 零假设下的成功概率

    Returns:
        双尾 p-value
    """
    if n <= 0:
        return 1.0

    try:
        from scipy.stats import binomtest

        result = binomtest(k, n, p0, alternative="two-sided")
        return float(result.pvalue)
    except ImportError:
        # 无 scipy 时退化为正态近似
        if n < 5:
            return 1.0
        expected = n * p0
        std = math.sqrt(n * p0 * (1 - p0))
        if std < 1e-12:
            return 1.0
        z = abs(k - expected) / std
        return 2.0 * (1.0 - _norm_cdf(z))


# ── Block Permutation ──────────────────────────────────────────


def block_shuffle(
    values: List[float], block_size: int, rng: random.Random
) -> List[float]:
    """Block permutation：按块打乱序列，保留局部自相关结构。

    这是所有排列检验的共用 shuffle 原语。
    各分析器不应各自实现 — 统一由此函数提供。

    Args:
        values: 原始序列
        block_size: 块大小（应由 auto_block_size 确定）
        rng: 随机数生成器

    Returns:
        打乱后的序列（长度与输入相同）
    """
    n = len(values)
    if block_size <= 0 or block_size >= n:
        result = list(values)
        rng.shuffle(result)
        return result
    blocks = [values[i : i + block_size] for i in range(0, n, block_size)]
    rng.shuffle(blocks)
    result: List[float] = []
    for b in blocks:
        result.extend(b)
    return result[:n]


# ── Newey-West 自相关一致标准误 ────────────────────────────────


def newey_west_se(
    residuals: List[float],
    max_lag: Optional[int] = None,
) -> float:
    """计算 Newey-West 自相关一致标准误 (HAC SE)。

    用于替代 OLS 标准误，当残差存在自相关时不会低估标准误。
    适用场景：IC 序列的 mean 估计、forward return 的 t 检验等。

    Args:
        residuals: 残差序列（或去均值后的序列）
        max_lag: 最大滞后阶数（None = 自动选择 floor(n^(1/3))）

    Returns:
        HAC 标准误估计
    """
    n = len(residuals)
    if n < 2:
        return 0.0

    if max_lag is None:
        max_lag = max(1, int(n ** (1.0 / 3.0)))

    # Γ(0) = (1/n) Σ e_t^2
    gamma_0 = sum(e * e for e in residuals) / n

    # Bartlett kernel weighted autocovariances
    nw_var = gamma_0
    for j in range(1, max_lag + 1):
        gamma_j = sum(residuals[t] * residuals[t - j] for t in range(j, n)) / n
        weight = 1.0 - j / (max_lag + 1.0)  # Bartlett kernel
        nw_var += 2.0 * weight * gamma_j

    # SE of mean = sqrt(NW_var / n)
    return math.sqrt(max(0.0, nw_var) / n)


# ── 内部工具 ──────────────────────────────────────────────────


def _norm_cdf(z: float) -> float:
    """标准正态 CDF（无需 scipy）。"""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """标准正态分位数函数（Beasley-Springer-Moro 近似）。"""
    if p <= 0.0:
        return -6.0
    if p >= 1.0:
        return 6.0
    if abs(p - 0.5) < 1e-15:
        return 0.0

    # Rational approximation (Abramowitz & Stegun 26.2.23)
    if p < 0.5:
        t = math.sqrt(-2.0 * math.log(p))
    else:
        t = math.sqrt(-2.0 * math.log(1.0 - p))

    c0 = 2.515517
    c1 = 0.802853
    c2 = 0.010328
    d1 = 1.432788
    d2 = 0.189269
    d3 = 0.001308

    z = t - (c0 + c1 * t + c2 * t * t) / (1.0 + d1 * t + d2 * t * t + d3 * t**3)
    return z if p > 0.5 else -z
