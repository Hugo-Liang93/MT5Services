"""策略信号相关性分析 — 从回测结果计算策略间方向一致性。

当两个策略在同一时间频繁发出相同方向信号时，它们提供的边际信息会下降。
此模块计算相关性矩阵，并输出 strategy_weights 建议，供 research/backtest
阶段做组合研究参考，不再对应 runtime 的 vote 配置。

设计原则：
- 纯函数，不依赖任何运行时组件
- 输入来自回测的 signal_evaluations 或 trades
- 输出用于研究态组合分析，不进入当前 runtime 单策略主链路
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class CorrelationPair:
    """一对策略的相关性分析结果。"""

    strategy_a: str
    strategy_b: str
    correlation: float
    overlap_count: int
    agreement_rate: float


@dataclass(frozen=True)
class CorrelationAnalysis:
    """策略相关性矩阵分析结果。"""

    pairs: List[CorrelationPair]
    strategy_weights: Dict[str, float]
    high_correlation_pairs: List[CorrelationPair]
    correlation_threshold: float
    total_bars_analyzed: int

    def to_dict(self) -> dict:
        return {
            "total_bars_analyzed": self.total_bars_analyzed,
            "correlation_threshold": self.correlation_threshold,
            "strategy_weights": dict(self.strategy_weights),
            "high_correlation_pairs": [
                {
                    "strategy_a": p.strategy_a,
                    "strategy_b": p.strategy_b,
                    "correlation": round(p.correlation, 4),
                    "overlap_count": p.overlap_count,
                    "agreement_rate": round(p.agreement_rate, 4),
                }
                for p in self.high_correlation_pairs
            ],
            "all_pairs_count": len(self.pairs),
        }


def analyze_strategy_correlation(
    signal_directions: Dict[str, List[int]],
    *,
    correlation_threshold: float = 0.80,
    penalty_weight: float = 0.50,
    min_overlap: int = 30,
) -> CorrelationAnalysis:
    """计算策略间信号方向的相关性矩阵。

    Args:
        signal_directions: {strategy_name: [+1, -1, 0, ...]} 按 bar 时间对齐的方向序列
            +1 = buy, -1 = sell, 0 = hold/no signal
        correlation_threshold: 相关性超过此值认为"高度相关"
        penalty_weight: 高度相关策略的降权值（如 0.50 = 权重减半）
        min_overlap: 两策略同时有信号（非0）的最小 bar 数

    Returns:
        CorrelationAnalysis 含相关性矩阵和推荐的 strategy_weights
    """
    strategies = sorted(signal_directions.keys())
    n = len(strategies)
    if n < 2:
        return CorrelationAnalysis(
            pairs=[],
            strategy_weights={s: 1.0 for s in strategies},
            high_correlation_pairs=[],
            correlation_threshold=correlation_threshold,
            total_bars_analyzed=len(next(iter(signal_directions.values()), [])),
        )

    total_bars = len(next(iter(signal_directions.values())))
    pairs: List[CorrelationPair] = []
    high_pairs: List[CorrelationPair] = []

    for i in range(n):
        for j in range(i + 1, n):
            sa = strategies[i]
            sb = strategies[j]
            va = signal_directions[sa]
            vb = signal_directions[sb]
            corr, overlap, agreement = _pearson_direction(va, vb, min_overlap)
            pair = CorrelationPair(
                strategy_a=sa,
                strategy_b=sb,
                correlation=corr,
                overlap_count=overlap,
                agreement_rate=agreement,
            )
            pairs.append(pair)
            if corr >= correlation_threshold:
                high_pairs.append(pair)

    # 计算 strategy_weights：被高相关对降权
    # 策略 A 和 B 相关性 > threshold → 保留交易次数更多的那个为 1.0，另一个降权
    weights: Dict[str, float] = {s: 1.0 for s in strategies}
    signal_counts = {s: sum(1 for v in signal_directions[s] if v != 0) for s in strategies}

    for pair in high_pairs:
        # 信号更多的策略保持 1.0，较少的降权
        if signal_counts.get(pair.strategy_a, 0) >= signal_counts.get(pair.strategy_b, 0):
            weights[pair.strategy_b] = min(weights[pair.strategy_b], penalty_weight)
        else:
            weights[pair.strategy_a] = min(weights[pair.strategy_a], penalty_weight)

    return CorrelationAnalysis(
        pairs=pairs,
        strategy_weights=weights,
        high_correlation_pairs=high_pairs,
        correlation_threshold=correlation_threshold,
        total_bars_analyzed=total_bars,
    )


def extract_signal_directions_from_evaluations(
    evaluations: List[Any],
    strategies: Optional[List[str]] = None,
) -> Dict[str, List[int]]:
    """从回测 signal_evaluations 提取对齐的方向序列。

    Args:
        evaluations: BacktestResult.signal_evaluations 列表
        strategies: 要分析的策略名子集（None = 全部）

    Returns:
        {strategy_name: [+1, -1, 0, ...]} 按 bar_time 排序对齐
    """
    by_bar: Dict[Any, Dict[str, int]] = defaultdict(dict)
    all_strategies: set[str] = set()

    for ev in evaluations:
        bar_time = getattr(ev, "bar_time", None) or ev.get("bar_time") if isinstance(ev, dict) else getattr(ev, "bar_time", None)
        strategy = getattr(ev, "strategy", None) or (ev.get("strategy") if isinstance(ev, dict) else None)
        direction = getattr(ev, "direction", None) or (ev.get("direction") if isinstance(ev, dict) else None)

        if bar_time is None or strategy is None:
            continue
        if strategies and strategy not in strategies:
            continue

        all_strategies.add(strategy)
        d = 0
        if direction == "buy":
            d = 1
        elif direction == "sell":
            d = -1
        by_bar[bar_time][strategy] = d

    sorted_bars = sorted(by_bar.keys())
    target_strategies = sorted(strategies) if strategies else sorted(all_strategies)

    result: Dict[str, List[int]] = {s: [] for s in target_strategies}
    for bar_time in sorted_bars:
        bar_signals = by_bar[bar_time]
        for s in target_strategies:
            result[s].append(bar_signals.get(s, 0))

    return result


def _pearson_direction(
    va: List[int], vb: List[int], min_overlap: int
) -> Tuple[float, int, float]:
    """计算两个方向序列的 Pearson 相关性。

    只在两者都非 0（都有信号）的 bar 上计算。

    Returns:
        (correlation, overlap_count, agreement_rate)
    """
    paired = [(a, b) for a, b in zip(va, vb) if a != 0 and b != 0]
    overlap = len(paired)
    if overlap < min_overlap:
        return 0.0, overlap, 0.0

    agreement = sum(1 for a, b in paired if a == b)
    agreement_rate = agreement / overlap

    # Pearson correlation
    a_vals = [float(a) for a, _ in paired]
    b_vals = [float(b) for _, b in paired]
    mean_a = sum(a_vals) / overlap
    mean_b = sum(b_vals) / overlap

    cov = sum((a - mean_a) * (b - mean_b) for a, b in zip(a_vals, b_vals))
    var_a = sum((a - mean_a) ** 2 for a in a_vals)
    var_b = sum((b - mean_b) ** 2 for b in b_vals)

    denom = math.sqrt(var_a * var_b)
    if denom < 1e-12:
        return 0.0, overlap, agreement_rate

    return cov / denom, overlap, agreement_rate
