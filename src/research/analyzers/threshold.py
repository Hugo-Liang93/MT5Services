"""阈值扫描分析器 — 发现指标的最优买卖阈值。

对给定指标字段，网格扫描阈值并计算每个阈值的：
  - 命中率（信号方向正确的比例）
  - 平均收益、Sharpe ratio
  - 盈亏比分解：avg_win / avg_loss / profit_factor
  - 期望值 (expectancy = hit_rate × avg_win - (1-hit_rate) × avg_loss)

包含时序交叉验证（expanding + sliding 双模式）和测试集外验证。
统计原语统一来自 statistics.py。
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

from src.signals.evaluation.regime import RegimeType

from ..core.config import OverfittingConfig, ThresholdSweepConfig
from ..core.data_matrix import DataMatrix
from ..core.contracts import ThresholdPoint, ThresholdSweepResult
from ..core.overfitting import (
    TimeSeriesFold,
    compute_cv_consistency,
    time_series_cv_splits,
)
from ..core.statistics import auto_block_size, block_shuffle

import random


def analyze_thresholds(
    matrix: DataMatrix,
    indicator_name: str,
    field_name: str,
    *,
    horizons: Optional[List[int]] = None,
    regime_filter: Optional[str] = None,
    config: Optional[ThresholdSweepConfig] = None,
    overfitting_config: Optional[OverfittingConfig] = None,
) -> List[ThresholdSweepResult]:
    """扫描指标阈值，找到最优买卖点。"""
    ts_cfg = config or ThresholdSweepConfig()
    of_cfg = overfitting_config or OverfittingConfig()

    if horizons is None:
        horizons = sorted(matrix.forward_returns.keys())

    series = matrix.indicator_series.get((indicator_name, field_name))
    if series is None:
        return []

    results: List[ThresholdSweepResult] = []
    for horizon in horizons:
        fwd = matrix.forward_returns.get(horizon)
        if fwd is None:
            continue
        result = _sweep_single_horizon(
            matrix=matrix,
            indicator_name=indicator_name,
            field_name=field_name,
            series=series,
            forward_returns=fwd,
            forward_bars=horizon,
            regime_filter=regime_filter,
            ts_cfg=ts_cfg,
            of_cfg=of_cfg,
        )
        if result is not None:
            results.append(result)

    return results


def _sweep_single_horizon(
    *,
    matrix: DataMatrix,
    indicator_name: str,
    field_name: str,
    series: List[Optional[float]],
    forward_returns: List[Optional[float]],
    forward_bars: int,
    regime_filter: Optional[str],
    ts_cfg: ThresholdSweepConfig,
    of_cfg: OverfittingConfig,
) -> Optional[ThresholdSweepResult]:
    """单个 horizon 的阈值扫描。"""
    train_range = matrix.train_slice()
    test_range = matrix.test_slice()

    train_pairs = _collect_pairs(
        series, forward_returns, matrix.regimes, train_range, regime_filter
    )
    if len(train_pairs) < of_cfg.min_samples:
        return None

    train_vals = [p[0] for p in train_pairs]

    thresholds = _generate_thresholds(train_vals, ts_cfg.sweep_points)
    if len(thresholds) < 3:
        return None

    # 扫描 buy_below 阈值
    buy_points: List[ThresholdPoint] = []
    for t in thresholds:
        pt = _evaluate_threshold(train_pairs, t, "buy_below", ts_cfg.target_metric)
        if pt is not None:
            buy_points.append(pt)

    # 扫描 sell_above 阈值
    sell_points: List[ThresholdPoint] = []
    for t in thresholds:
        pt = _evaluate_threshold(train_pairs, t, "sell_above", ts_cfg.target_metric)
        if pt is not None:
            sell_points.append(pt)

    # 找最优
    best_buy = _find_best(buy_points, ts_cfg.target_metric, of_cfg.min_samples)
    best_sell = _find_best(sell_points, ts_cfg.target_metric, of_cfg.min_samples)

    # 交叉验证一致性
    cv_buy = _cv_consistency(
        series, forward_returns, matrix.regimes, train_range,
        regime_filter, thresholds, "buy_below", ts_cfg, of_cfg,
    )
    cv_sell = _cv_consistency(
        series, forward_returns, matrix.regimes, train_range,
        regime_filter, thresholds, "sell_above", ts_cfg, of_cfg,
    )

    # 测试集验证
    test_pairs = _collect_pairs(
        series, forward_returns, matrix.regimes, test_range, regime_filter
    )
    test_buy_hr, test_buy_n = _validate_threshold(
        test_pairs, best_buy.threshold if best_buy else None, "buy_below",
    )
    test_sell_hr, test_sell_n = _validate_threshold(
        test_pairs, best_sell.threshold if best_sell else None, "sell_above",
    )

    all_points = buy_points + sell_points

    # 排列检验 — 自适应 block size
    perm_p_buy: Optional[float] = None
    perm_p_sell: Optional[float] = None
    is_sig_buy = False
    is_sig_sell = False

    if ts_cfg.n_permutations > 0:
        fwd_vals = [p[1] for p in train_pairs]
        adaptive_bs = auto_block_size(fwd_vals)

        if best_buy is not None:
            best_buy_score = _metric_score(best_buy, ts_cfg.target_metric)
            perm_p_buy = _permutation_test_threshold(
                train_pairs, best_buy_score, "buy_below", thresholds,
                ts_cfg.target_metric, n_permutations=ts_cfg.n_permutations,
                block_size=adaptive_bs,
            )
            is_sig_buy = (
                perm_p_buy <= ts_cfg.permutation_significance
                and cv_buy >= of_cfg.cv_consistency_threshold
            )

        if best_sell is not None:
            best_sell_score = _metric_score(best_sell, ts_cfg.target_metric)
            perm_p_sell = _permutation_test_threshold(
                train_pairs, best_sell_score, "sell_above", thresholds,
                ts_cfg.target_metric, n_permutations=ts_cfg.n_permutations,
                block_size=adaptive_bs,
            )
            is_sig_sell = (
                perm_p_sell <= ts_cfg.permutation_significance
                and cv_sell >= of_cfg.cv_consistency_threshold
            )

    return ThresholdSweepResult(
        indicator_name=indicator_name,
        field_name=field_name,
        forward_bars=forward_bars,
        regime=regime_filter,
        optimal_buy_threshold=best_buy.threshold if best_buy else None,
        buy_hit_rate=best_buy.hit_rate if best_buy else 0.0,
        buy_mean_return=best_buy.mean_return if best_buy else 0.0,
        buy_n_signals=best_buy.n_signals if best_buy else 0,
        optimal_sell_threshold=best_sell.threshold if best_sell else None,
        sell_hit_rate=best_sell.hit_rate if best_sell else 0.0,
        sell_mean_return=best_sell.mean_return if best_sell else 0.0,
        sell_n_signals=best_sell.n_signals if best_sell else 0,
        sweep_points=all_points,
        cv_consistency_buy=cv_buy,
        cv_consistency_sell=cv_sell,
        test_buy_hit_rate=test_buy_hr,
        test_sell_hit_rate=test_sell_hr,
        test_buy_n_signals=test_buy_n,
        test_sell_n_signals=test_sell_n,
        is_significant_buy=is_sig_buy,
        is_significant_sell=is_sig_sell,
        permutation_p_buy=perm_p_buy,
        permutation_p_sell=perm_p_sell,
    )


def _collect_pairs(
    series: List[Optional[float]],
    forward_returns: List[Optional[float]],
    regimes: List[RegimeType],
    idx_range: range,
    regime_filter: Optional[str],
) -> List[Tuple[float, float]]:
    """收集对齐的 (indicator_value, forward_return) 对。"""
    pairs: List[Tuple[float, float]] = []
    for i in idx_range:
        iv = series[i]
        fv = forward_returns[i]
        if iv is None or fv is None:
            continue
        if regime_filter is not None and regimes[i].value != regime_filter:
            continue
        pairs.append((iv, fv))
    return pairs


def _generate_thresholds(values: List[float], n_points: int) -> List[float]:
    """基于数据分位数生成阈值网格。"""
    if not values or n_points < 2:
        return []
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    lo_idx = max(0, int(n * 0.05))
    hi_idx = min(n - 1, int(n * 0.95))
    lo = sorted_vals[lo_idx]
    hi = sorted_vals[hi_idx]
    if hi - lo < 1e-12:
        return []
    step = (hi - lo) / (n_points - 1)
    return [lo + i * step for i in range(n_points)]


def _evaluate_threshold(
    pairs: List[Tuple[float, float]],
    threshold: float,
    direction: str,
    target_metric: str,
) -> Optional[ThresholdPoint]:
    """评估单个阈值的表现（含信号聚集去重 + 盈亏比分解）。"""
    signals: List[Tuple[float, float]] = []
    prev_triggered = False
    for iv, fv in pairs:
        triggered = (iv <= threshold) if direction == "buy_below" else (iv >= threshold)
        if triggered and not prev_triggered:
            signals.append((iv, fv))
        prev_triggered = triggered

    n = len(signals)
    if n < 5:
        return None

    returns = [fv for _, fv in signals]
    if direction == "sell_above":
        returns = [-fv for _, fv in signals]

    hits = sum(1 for r in returns if r > 0)
    hit_rate = hits / n
    mean_ret = sum(returns) / n

    # 盈亏比分解
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    total_wins = sum(wins)
    total_losses = abs(sum(losses))
    profit_factor = total_wins / total_losses if total_losses > 1e-12 else 0.0

    # Sharpe（简化版：mean / std）
    if n > 1:
        variance = sum((r - mean_ret) ** 2 for r in returns) / (n - 1)
        std = math.sqrt(variance) if variance > 0 else 0.0
        sharpe = mean_ret / std if std > 1e-12 else 0.0
    else:
        sharpe = 0.0

    return ThresholdPoint(
        threshold=threshold,
        direction=direction,
        hit_rate=hit_rate,
        mean_return=mean_ret,
        n_signals=n,
        sharpe=sharpe,
        avg_win=avg_win,
        avg_loss=avg_loss,
        profit_factor=profit_factor,
    )


def _find_best(
    points: List[ThresholdPoint],
    metric: str,
    min_signals: int,
) -> Optional[ThresholdPoint]:
    """找到最优阈值点。"""
    valid = [p for p in points if p.n_signals >= min_signals]
    if not valid:
        return None

    return max(valid, key=lambda p: _metric_score(p, metric))


def _metric_score(pt: ThresholdPoint, metric: str) -> float:
    """提取阈值点的目标 metric 分数。"""
    if metric == "hit_rate":
        return pt.hit_rate
    elif metric == "mean_return":
        return pt.mean_return
    elif metric == "sharpe":
        return pt.sharpe
    else:
        # expectancy = hit_rate × avg_win - (1 - hit_rate) × avg_loss
        # 这比直接用 mean_return 多提供了盈亏比维度的信息
        return pt.hit_rate * pt.avg_win - (1 - pt.hit_rate) * pt.avg_loss


def _cv_consistency(
    series: List[Optional[float]],
    forward_returns: List[Optional[float]],
    regimes: List[RegimeType],
    train_range: range,
    regime_filter: Optional[str],
    thresholds: List[float],
    direction: str,
    ts_cfg: ThresholdSweepConfig,
    of_cfg: OverfittingConfig,
) -> float:
    """计算交叉验证一致性。"""
    folds = time_series_cv_splits(
        n_samples=len(train_range),
        n_folds=of_cfg.cv_folds,
        min_train_size=max(of_cfg.min_samples, 50),
        mode=of_cfg.cv_mode,
    )
    if not folds:
        return 0.0

    fold_bests: List[Optional[float]] = []
    base_start = train_range.start

    for fold in folds:
        fold_train_range = range(
            base_start + fold.train_start, base_start + fold.train_end
        )
        fold_pairs = _collect_pairs(
            series, forward_returns, regimes, fold_train_range, regime_filter,
        )
        if len(fold_pairs) < of_cfg.min_samples:
            fold_bests.append(None)
            continue

        best_point: Optional[ThresholdPoint] = None
        best_score = float("-inf")
        for t in thresholds:
            pt = _evaluate_threshold(fold_pairs, t, direction, ts_cfg.target_metric)
            if pt is None or pt.n_signals < 5:
                continue
            score = _metric_score(pt, ts_cfg.target_metric)
            if score > best_score:
                best_score = score
                best_point = pt

        fold_bests.append(best_point.threshold if best_point else None)

    return compute_cv_consistency(fold_bests)


def _validate_threshold(
    test_pairs: List[Tuple[float, float]],
    threshold: Optional[float],
    direction: str,
) -> Tuple[Optional[float], int]:
    """在测试集上验证阈值的表现。"""
    if threshold is None or not test_pairs:
        return None, 0

    if direction == "buy_below":
        signals = [(iv, fv) for iv, fv in test_pairs if iv <= threshold]
    else:
        signals = [(iv, fv) for iv, fv in test_pairs if iv >= threshold]

    n = len(signals)
    if n == 0:
        return None, 0

    returns = [fv for _, fv in signals]
    if direction == "sell_above":
        returns = [-fv for _, fv in signals]

    hits = sum(1 for r in returns if r > 0)
    return hits / n, n


def _permutation_test_threshold(
    train_pairs: List[Tuple[float, float]],
    best_metric_score: float,
    direction: str,
    thresholds: List[float],
    target_metric: str,
    n_permutations: int = 200,
    seed: int = 42,
    block_size: int = 10,
) -> float:
    """排列检验：打乱 forward_returns，重新扫描取 max，得到 null distribution。

    block_size 由调用方通过 auto_block_size() 自适应确定。
    """
    if not train_pairs or not thresholds:
        return 1.0

    rng = random.Random(seed)
    ind_vals = [p[0] for p in train_pairs]
    fwd_vals = [p[1] for p in train_pairs]
    count_ge = 0

    for _ in range(n_permutations):
        shuffled_fwd = block_shuffle(fwd_vals, block_size, rng)
        shuffled_pairs = list(zip(ind_vals, shuffled_fwd))

        perm_best_score = float("-inf")
        for t in thresholds:
            pt = _evaluate_threshold(shuffled_pairs, t, direction, target_metric)
            if pt is None:
                continue
            score = _metric_score(pt, target_metric)
            if score > perm_best_score:
                perm_best_score = score

        if perm_best_score >= best_metric_score:
            count_ge += 1

    return count_ge / max(n_permutations, 1)
