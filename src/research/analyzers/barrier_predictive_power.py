"""Barrier 预测力分析器 — IC 基于 Triple-Barrier 真实出场收益。

替代 predictive_power.py 的短 forward_return 语义——y 是 Triple-Barrier 模拟
的真实 tp/sl/time 出场收益（`DataMatrix.barrier_returns_{long,short}`），与实盘
Chandelier exit 模型同构。

解决 docs/research-system.md 顶部记录的"血教训"：短 FR IC 与实盘 trailing 胜率
几乎无相关性，这个 analyzer 让挖掘阶段的 IC 直接对应可交易 barrier 配置。

设计约束：
  - v1 仅做 regime=None（全样本）分析；per_regime 留给 v2
    —— 因为 per_regime × 9 barrier × 2 direction × ~100 indicator 会膨胀到数万，
    permutation 成本不可接受
  - BH-FDR 全局批量校正
  - 复用 PermutationEngine spearman 特化路径（pre-rank 7x 加速）
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from scipy import stats as scipy_stats

from ..core.config import OverfittingConfig, PredictivePowerConfig
from ..core.contracts import IndicatorBarrierPredictiveResult
from ..core.data_matrix import DataMatrix
from ..core.overfitting import benjamini_hochberg_fdr
from ..core.permutation import PermutationTestSpec, run_permutation_test
from ..core.statistics import auto_block_size

logger = logging.getLogger(__name__)


def _compute_exit_breakdown(
    outcomes: List[Optional[Any]],
    indices: List[int],
) -> Tuple[float, float, float, float, float]:
    """从 BarrierOutcome 序列统计 tp/sl/time hit rates + mean_bars_held + mean_return."""
    n = len(indices)
    if n == 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    tp = sl = time_ = 0
    bars_held_sum = 0.0
    return_sum = 0.0
    for i in indices:
        o = outcomes[i]
        if o is None:
            continue
        if o.barrier == "tp":
            tp += 1
        elif o.barrier == "sl":
            sl += 1
        else:  # "time"
            time_ += 1
        bars_held_sum += o.bars_held
        return_sum += o.return_pct
    return (
        tp / n,
        sl / n,
        time_ / n,
        bars_held_sum / n,
        return_sum / n,
    )


def _analyze_single(
    *,
    ind_name: str,
    field_name: str,
    direction: str,
    barrier_key: tuple,
    feature_series: List[Optional[float]],
    outcomes: List[Optional[Any]],
    train_indices: List[int],
    min_samples: int,
    n_permutations: int,
) -> Optional[IndicatorBarrierPredictiveResult]:
    """对单个 (ind, fld, barrier, direction) 计算 barrier IC。"""
    # 对齐：保留 feature 和 outcome 都非 None 的 train indices
    valid_indices: List[int] = []
    feats: List[float] = []
    rets: List[float] = []
    for i in train_indices:
        fv = feature_series[i]
        ov = outcomes[i]
        if fv is None or ov is None:
            continue
        valid_indices.append(i)
        feats.append(float(fv))
        rets.append(float(ov.return_pct))

    n = len(valid_indices)
    if n < min_samples:
        return None

    feats_arr = np.asarray(feats, dtype=np.float64)
    rets_arr = np.asarray(rets, dtype=np.float64)

    if float(np.std(feats_arr)) < 1e-12 or float(np.std(rets_arr)) < 1e-12:
        return None

    pearson_r, pearson_p = scipy_stats.pearsonr(feats_arr, rets_arr)
    spearman_rho, spearman_p = scipy_stats.spearmanr(feats_arr, rets_arr)

    ic = float(spearman_rho)
    parametric_p = float(min(pearson_p, spearman_p))

    # 排列检验（block shuffle）
    perm_p: Optional[float] = None
    if n_permutations > 0:
        block_size = auto_block_size(rets)
        spec = PermutationTestSpec(
            ind_vals=tuple(feats),
            fwd_vals=tuple(rets),
            observed_ic=ic,
            n_permutations=n_permutations,
            block_size=max(block_size, 1),
            seed=42,
        )
        perm_result = run_permutation_test(spec)
        perm_p = float(perm_result.p_value)

    tp_rate, sl_rate, time_rate, mean_bars, mean_ret = _compute_exit_breakdown(
        outcomes, valid_indices
    )

    return IndicatorBarrierPredictiveResult(
        indicator_name=ind_name,
        field_name=field_name,
        regime=None,
        direction=direction,
        barrier_key=barrier_key,
        n_samples=n,
        pearson_r=float(pearson_r),
        spearman_rho=float(spearman_rho),
        information_coefficient=ic,
        p_value=parametric_p,
        permutation_p_value=perm_p,
        is_significant=False,  # 在 BH-FDR 阶段后更新
        tp_hit_rate=tp_rate,
        sl_hit_rate=sl_rate,
        time_exit_rate=time_rate,
        mean_bars_held=mean_bars,
        mean_return_pct=mean_ret,
    )


def analyze_barrier_predictive_power(
    matrix: DataMatrix,
    *,
    indicator_fields: Optional[List[Tuple[str, str]]] = None,
    config: Optional[PredictivePowerConfig] = None,
    overfitting_config: Optional[OverfittingConfig] = None,
    use_train_only: bool = True,
) -> List[IndicatorBarrierPredictiveResult]:
    """对 (indicator_field, barrier_config, direction) 计算 barrier IC。

    y 使用 Triple-Barrier 模拟的真实出场收益，与实盘 exit 模型同构。

    Args:
        matrix: DataMatrix（须含 barrier_returns_long/short；由 build_data_matrix 自动填充）
        indicator_fields: 仅分析这些字段；None = 所有无量纲字段
        config: PredictivePowerConfig（复用 n_permutations / significance_level）
        overfitting_config: min_samples / significance_level
        use_train_only: True = 仅在 train slice 上计算（默认）

    Returns:
        IndicatorBarrierPredictiveResult 列表（BH-FDR 已批量校正显著性）
    """
    pp_cfg = config or PredictivePowerConfig()
    of_cfg = overfitting_config or OverfittingConfig()

    # 前置：barrier_returns 必须已填充
    if not matrix.barrier_returns_long and not matrix.barrier_returns_short:
        logger.warning(
            "analyze_barrier_predictive_power: matrix.barrier_returns_{long,short} "
            "均为空。确认 build_data_matrix 是否注入 atr14 指标。"
        )
        return []

    if indicator_fields is None:
        from src.research.analyzers.rule_mining import _is_dimensionless

        indicator_fields = [
            (ind, fld)
            for ind, fld in matrix.available_indicator_fields()
            if _is_dimensionless(ind, fld)
        ]

    train_indices = list(matrix.train_slice()) if use_train_only else list(
        range(matrix.n_bars)
    )

    # Phase 1: 逐组合计算 raw results
    raw_results: List[IndicatorBarrierPredictiveResult] = []

    directions_and_dicts = [
        ("long", matrix.barrier_returns_long),
        ("short", matrix.barrier_returns_short),
    ]

    for ind_name, field_name in indicator_fields:
        feature_series = matrix.indicator_series.get((ind_name, field_name))
        if feature_series is None:
            continue

        for direction, barrier_dict in directions_and_dicts:
            for barrier_key, outcomes in barrier_dict.items():
                result = _analyze_single(
                    ind_name=ind_name,
                    field_name=field_name,
                    direction=direction,
                    barrier_key=barrier_key,
                    feature_series=feature_series,
                    outcomes=outcomes,
                    train_indices=train_indices,
                    min_samples=of_cfg.min_samples,
                    n_permutations=pp_cfg.n_permutations,
                )
                if result is not None:
                    raw_results.append(result)

    if not raw_results:
        return []

    # Phase 2: BH-FDR 批量校正（优先用 permutation p-value）
    p_values = [
        (r.permutation_p_value if r.permutation_p_value is not None else r.p_value)
        for r in raw_results
    ]
    corrected = benjamini_hochberg_fdr(
        p_values, alpha=pp_cfg.significance_level, n_total_tests=len(p_values)
    )

    final_results: List[IndicatorBarrierPredictiveResult] = []
    for idx, (orig_idx, _adj_p, is_sig) in enumerate(corrected):
        raw = raw_results[orig_idx]
        # frozen dataclass → replace 构造新实例
        from dataclasses import replace

        final_results.append(replace(raw, is_significant=bool(is_sig)))

    # 按 |IC| 降序输出（与 predictive_power 对齐）
    final_results.sort(key=lambda r: abs(r.information_coefficient), reverse=True)
    return final_results
