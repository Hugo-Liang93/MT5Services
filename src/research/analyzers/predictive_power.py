"""指标预测力分析器 — 评估每个指标字段对未来收益的预测能力。

对每个 (indicator_field, forward_horizon, regime) 组合计算：
  - Pearson / Spearman 相关系数
  - Information Coefficient (IC = Spearman rho)
  - 方向命中率（指标 > 中位数时正收益比例）
  - 统计显著性（BH-FDR 批量校正 或 Bonferroni）
  - Rolling IC + Information Ratio（时序稳定性）
  - 排列检验 p-value（null distribution 下的显著性）

两阶段架构：
  Phase 1: 逐个计算 raw result（含 raw p_value，不判断显著性）
  Phase 2: 收集全部 p_values，批量校正，标记 is_significant

纯函数设计，不依赖运行时组件。
"""

from __future__ import annotations

import math
import random
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

from scipy import stats as scipy_stats

from src.signals.evaluation.regime import RegimeType

from ..config import OverfittingConfig, PredictivePowerConfig
from ..data_matrix import DataMatrix
from ..models import IndicatorPredictiveResult, RollingICResult
from ..overfitting import (
    benjamini_hochberg_fdr,
    bonferroni_correction,
    check_significance,
)


def analyze_predictive_power(
    matrix: DataMatrix,
    *,
    horizons: Optional[List[int]] = None,
    indicator_fields: Optional[List[Tuple[str, str]]] = None,
    config: Optional[PredictivePowerConfig] = None,
    overfitting_config: Optional[OverfittingConfig] = None,
    use_train_only: bool = True,
) -> List[IndicatorPredictiveResult]:
    """分析指标字段的预测力。

    两阶段架构：先计算 raw results，再批量校正显著性。
    """
    pp_cfg = config or PredictivePowerConfig()
    of_cfg = overfitting_config or OverfittingConfig()

    if horizons is None:
        horizons = sorted(matrix.forward_returns.keys())
    if indicator_fields is None:
        # 默认只分析无量纲字段（排除绝对价格字段如 bb_upper, sma, ema 等）
        from src.research.analyzers.rule_mining import _is_dimensionless

        indicator_fields = [
            (ind, fld)
            for ind, fld in matrix.available_indicator_fields()
            if _is_dimensionless(ind, fld)
        ]

    idx_range = matrix.train_slice() if use_train_only else range(matrix.n_bars)

    regime_list: List[Optional[str]] = [None]
    if pp_cfg.per_regime:
        regime_list.extend([r.value for r in RegimeType])

    # ── Phase 1: 逐个计算 raw results ──────────────────────────
    raw_results: List[IndicatorPredictiveResult] = []

    for ind_name, field_name in indicator_fields:
        series = matrix.indicator_series.get((ind_name, field_name))
        if series is None:
            continue

        for horizon in horizons:
            fwd = matrix.forward_returns.get(horizon)
            if fwd is None:
                continue

            for regime_filter in regime_list:
                result = _compute_single(
                    ind_name=ind_name,
                    field_name=field_name,
                    forward_bars=horizon,
                    regime_filter=regime_filter,
                    indicator_values=series,
                    forward_returns=fwd,
                    regimes=matrix.regimes,
                    idx_range=idx_range,
                    of_cfg=of_cfg,
                    pp_cfg=pp_cfg,
                )
                if result is not None:
                    raw_results.append(result)

    # ── Phase 2: 批量显著性校正 ────────────────────────────────
    results = _apply_batch_correction(raw_results, of_cfg, pp_cfg.significance_level)

    results.sort(key=lambda r: abs(r.information_coefficient), reverse=True)
    return results


def _compute_single(
    *,
    ind_name: str,
    field_name: str,
    forward_bars: int,
    regime_filter: Optional[str],
    indicator_values: List[Optional[float]],
    forward_returns: List[Optional[float]],
    regimes: List[RegimeType],
    idx_range: range,
    of_cfg: OverfittingConfig,
    pp_cfg: PredictivePowerConfig,
) -> Optional[IndicatorPredictiveResult]:
    """计算单个 (indicator, horizon, regime) 组合，返回 raw result。"""
    # 收集对齐的 (indicator_value, forward_return) 对
    ind_vals: List[float] = []
    fwd_vals: List[float] = []

    for i in idx_range:
        iv = indicator_values[i]
        fv = forward_returns[i]
        if iv is None or fv is None:
            continue
        if regime_filter is not None and regimes[i].value != regime_filter:
            continue
        ind_vals.append(iv)
        fwd_vals.append(fv)

    n = len(ind_vals)
    if n < of_cfg.min_samples:
        return None

    if _is_constant(ind_vals) or _is_constant(fwd_vals):
        return None

    # Pearson 相关
    try:
        pearson_r, pearson_p = scipy_stats.pearsonr(ind_vals, fwd_vals)
    except Exception:
        pearson_r, pearson_p = 0.0, 1.0

    # Spearman 秩相关 (= Information Coefficient)
    try:
        spearman_rho, spearman_p = scipy_stats.spearmanr(ind_vals, fwd_vals)
    except Exception:
        spearman_rho, spearman_p = 0.0, 1.0

    # 取两个检验中较小的 p-value，× 2 校正多重检验（Bonferroni on 2 tests）
    p_value = min(min(pearson_p, spearman_p) * 2.0, 1.0)

    # 方向命中率
    median_ind = _median(ind_vals)
    above_positive = 0
    above_total = 0
    below_positive = 0
    below_total = 0
    for iv, fv in zip(ind_vals, fwd_vals):
        if iv >= median_ind:
            above_total += 1
            if fv > 0:
                above_positive += 1
        else:
            below_total += 1
            if fv > 0:
                below_positive += 1

    hit_above = above_positive / above_total if above_total > 0 else 0.5
    hit_below = below_positive / below_total if below_total > 0 else 0.5
    ic = float(spearman_rho)

    # Rolling IC（可选）
    rolling_ic: Optional[RollingICResult] = None
    if pp_cfg.rolling_ic_enabled:
        rolling_ic = _compute_rolling_ic(
            ind_vals, fwd_vals, window_size=pp_cfg.rolling_ic_window
        )

    # 排列检验（可选）
    permutation_p: Optional[float] = None
    if pp_cfg.permutation_test_enabled:
        permutation_p = _permutation_test_ic(
            ind_vals, fwd_vals, ic, n_permutations=pp_cfg.n_permutations
        )

    return IndicatorPredictiveResult(
        indicator_name=ind_name,
        field_name=field_name,
        forward_bars=forward_bars,
        regime=regime_filter,
        n_samples=n,
        pearson_r=float(pearson_r),
        spearman_rho=float(spearman_rho),
        p_value=float(p_value),
        hit_rate_above_median=hit_above,
        hit_rate_below_median=hit_below,
        information_coefficient=ic,
        is_significant=False,  # Phase 2 会覆盖
        rolling_ic=rolling_ic,
        permutation_p_value=permutation_p,
    )


def _apply_batch_correction(
    results: List[IndicatorPredictiveResult],
    of_cfg: OverfittingConfig,
    significance_level: float,
) -> List[IndicatorPredictiveResult]:
    """批量校正显著性：BH-FDR 或 Bonferroni。"""
    if not results:
        return results

    n_tests = len(results)

    if of_cfg.correction_method == "bh_fdr":
        p_values = [r.p_value for r in results]
        fdr_results = benjamini_hochberg_fdr(p_values, alpha=significance_level)

        corrected: List[IndicatorPredictiveResult] = []
        for (orig_idx, adj_p, is_sig), r in zip(fdr_results, results):
            # 额外检查效应量门槛
            hit_dev = max(
                abs(r.hit_rate_above_median - 0.5),
                abs(r.hit_rate_below_median - 0.5),
            )
            passes_effect_size = (
                abs(r.information_coefficient) >= of_cfg.min_correlation
                or hit_dev >= of_cfg.min_hit_rate_deviation
            )
            corrected.append(replace(r, is_significant=is_sig and passes_effect_size))
        return corrected
    else:
        # Bonferroni 模式：逐个校正
        corrected = []
        for r in results:
            hit_dev = max(
                abs(r.hit_rate_above_median - 0.5),
                abs(r.hit_rate_below_median - 0.5),
            )
            is_sig, _ = check_significance(
                p_value=r.p_value,
                n_samples=r.n_samples,
                correlation=r.information_coefficient,
                hit_rate_deviation=hit_dev,
                significance_level=significance_level,
                min_samples=of_cfg.min_samples,
                min_correlation=of_cfg.min_correlation,
                min_hit_rate_deviation=of_cfg.min_hit_rate_deviation,
                n_tests=n_tests,
                apply_bonferroni=True,
            )
            corrected.append(replace(r, is_significant=is_sig))
        return corrected


# ── Rolling IC ─────────────────────────────────────────────────


def _compute_rolling_ic(
    ind_vals: List[float],
    fwd_vals: List[float],
    window_size: int = 60,
    min_window_samples: int = 20,
) -> Optional[RollingICResult]:
    """滑动窗口计算 IC，返回 mean/std/IR。"""
    n = len(ind_vals)
    if n < window_size:
        return None

    ics: List[float] = []
    # 步长 = max(1, window_size // 10)，足够细以捕捉 IC 衰减拐点
    step = max(1, window_size // 10)
    for start in range(0, n - window_size + 1, step):
        end = start + window_size
        win_ind = ind_vals[start:end]
        win_fwd = fwd_vals[start:end]

        valid = [(iv, fv) for iv, fv in zip(win_ind, win_fwd)]
        if len(valid) < min_window_samples:
            continue
        if _is_constant([v[0] for v in valid]) or _is_constant([v[1] for v in valid]):
            continue

        try:
            rho, _ = scipy_stats.spearmanr([v[0] for v in valid], [v[1] for v in valid])
            ics.append(float(rho))
        except Exception:
            continue

    if len(ics) < 3:
        return None

    mean_ic = sum(ics) / len(ics)
    variance = sum((ic - mean_ic) ** 2 for ic in ics) / (len(ics) - 1)
    std_ic = math.sqrt(variance) if variance > 0 else 0.0
    ir = mean_ic / std_ic if std_ic > 1e-12 else 0.0

    return RollingICResult(
        mean_ic=mean_ic,
        std_ic=std_ic,
        information_ratio=ir,
        n_windows=len(ics),
    )


# ── 排列检验 ──────────────────────────────────────────────────


def _block_shuffle(
    values: List[float], block_size: int, rng: random.Random
) -> List[float]:
    """Block permutation：按块打乱，保留局部自相关结构。"""
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


def _permutation_test_ic(
    ind_vals: List[float],
    fwd_vals: List[float],
    observed_ic: float,
    n_permutations: int = 1000,
    seed: int = 42,
    block_size: int = 10,
) -> float:
    """Block permutation 检验：按块打乱 forward_returns，保留局部自相关。

    Returns:
        p-value: |permuted_ic| >= |observed_ic| 的比例
    """
    rng = random.Random(seed)
    abs_observed = abs(observed_ic)
    count_ge = 0

    for _ in range(n_permutations):
        shuffled = _block_shuffle(fwd_vals, block_size, rng)
        try:
            rho, _ = scipy_stats.spearmanr(ind_vals, shuffled)
            if abs(rho) >= abs_observed:
                count_ge += 1
        except Exception:
            continue

    return count_ge / max(n_permutations, 1)


# ── 工具函数 ──────────────────────────────────────────────────


def _median(values: List[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2.0
    return s[mid]


def _is_constant(values: List[float], tol: float = 1e-12) -> bool:
    if len(values) < 2:
        return True
    v0 = values[0]
    return all(abs(v - v0) < tol for v in values)
