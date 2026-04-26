"""策略相关性分析工具 — 运行回测收集信号方向，计算策略间相关性矩阵。

用法：
    python -m src.ops.cli.correlation_runner --tf H1
    python -m src.ops.cli.correlation_runner --tf M30 --threshold 0.70
    python -m src.ops.cli.correlation_runner --tf H1,M30 --start 2025-12-30 --end 2026-03-30

输出：
    - 高相关策略对（超过阈值的 pair）
    - 推荐 strategy_weights（可直接写入 signal.ini [voting_group.*.weights]）
    - 相关性矩阵热力图（文本形式）

设计：
    复用 BacktestEngine 跑完整回测，从 signal_evaluations 提取各策略的方向序列，
    然后调用 analyze_strategy_correlation() 纯函数。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import warnings

warnings.filterwarnings("ignore")

import logging

logging.disable(logging.CRITICAL)

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _collect_signal_directions(
    tf: str,
    start: str,
    end: str,
    *,
    config_overrides: Optional[Dict[str, Any]] = None,
) -> tuple:
    """运行回测并收集各策略的信号方向序列。

    Returns:
        (signal_directions, backtest_result_data)
        signal_directions: {strategy: [+1, -1, 0, ...]}
    """
    from src.backtesting.component_factory import (
        _load_signal_config_snapshot,
        build_backtest_components,
    )
    from src.backtesting.config import get_backtest_defaults
    from src.backtesting.engine import BacktestEngine
    from src.backtesting.models import BacktestConfig

    _OPTIMIZER_ONLY = {"search_mode", "max_combinations", "sort_metric"}
    defaults = {k: v for k, v in get_backtest_defaults().items() if k not in _OPTIMIZER_ONLY}
    signal_config = _load_signal_config_snapshot()

    strategy_sessions: dict = {}
    for name, sess_list in getattr(signal_config, "strategy_sessions", {}).items():
        if sess_list:
            strategy_sessions[name] = (
                list(sess_list) if not isinstance(sess_list, list) else sess_list
            )
    strategy_timeframes: dict = {}
    for name, tf_list in getattr(signal_config, "strategy_timeframes", {}).items():
        if tf_list:
            strategy_timeframes[name] = (
                list(tf_list) if not isinstance(tf_list, list) else tf_list
            )

    merged = dict(defaults)
    # 确保记录信号评估（需要大量评估数据来计算相关性）
    merged["max_signal_evaluations"] = 0  # 不限制
    if config_overrides:
        merged.update(config_overrides)

    config = BacktestConfig.from_flat(
        symbol="XAUUSD",
        timeframe=tf,
        start_time=datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
        end_time=datetime.fromisoformat(end).replace(tzinfo=timezone.utc),
        strategy_sessions=strategy_sessions,
        strategy_timeframes=strategy_timeframes,
        **merged,
    )

    components = build_backtest_components()
    engine = BacktestEngine(
        config=config,
        data_loader=components["data_loader"],
        signal_module=components["signal_module"],
        indicator_pipeline=components["pipeline"],
        regime_detector=components["regime_detector"],
        performance_tracker=components.get("performance_tracker"),
    )
    result = engine.run()

    # 从 signal_evaluations 提取方向序列
    evaluations = result.signal_evaluations or []
    if not evaluations:
        return {}, result

    # 按 bar_time 排序，构建 {strategy: [direction, ...]}
    bar_times: list = sorted(set(e.bar_time for e in evaluations))
    bar_time_index = {t: i for i, t in enumerate(bar_times)}
    n_bars = len(bar_times)

    # 初始化方向矩阵
    strategies: set = set(e.strategy for e in evaluations)
    directions: Dict[str, List[int]] = {
        s: [0] * n_bars for s in strategies
    }

    for e in evaluations:
        idx = bar_time_index[e.bar_time]
        if e.direction == "buy":
            directions[e.strategy][idx] = 1
        elif e.direction == "sell":
            directions[e.strategy][idx] = -1

    return directions, result


def _render_correlation_result(
    analysis: Any,
    tf: str,
    n_bars: int,
) -> str:
    """渲染相关性分析结果为文本。"""
    lines = [
        f"\n{'='*70}",
        f" Strategy Correlation Analysis — {tf} XAUUSD ({n_bars} bars analyzed)",
        f"{'='*70}",
    ]

    # 高相关对
    high_pairs = analysis.high_correlation_pairs
    if high_pairs:
        lines.append(f"\n--- High Correlation Pairs (>{analysis.correlation_threshold:.2f}) ---")
        lines.append(
            f"  {'Strategy A':<28} {'Strategy B':<28} {'Corr':>6} {'Overlap':>8} {'Agree%':>7}"
        )
        for p in sorted(high_pairs, key=lambda x: x.correlation, reverse=True):
            lines.append(
                f"  {p.strategy_a:<28} {p.strategy_b:<28} {p.correlation:>6.3f} "
                f"{p.overlap_count:>8} {p.agreement_rate:>6.1%}"
            )
    else:
        lines.append("\n  No high-correlation pairs found (all pairs below threshold).")

    # 推荐权重
    lines.append(f"\n--- Recommended Strategy Weights ---")
    lines.append(f"  (1.00 = keep as-is, <1.00 = reduce due to redundancy)")
    for name, weight in sorted(
        analysis.strategy_weights.items(), key=lambda x: x[1]
    ):
        marker = " *** REDUCE" if weight < 1.0 else ""
        lines.append(f"  {name:<28} weight={weight:.2f}{marker}")

    # 建议
    reduced = [n for n, w in analysis.strategy_weights.items() if w < 1.0]
    if reduced:
        lines.append(f"\n--- Action Items ---")
        lines.append(f"  {len(reduced)} strategies recommended for weight reduction:")
        for name in reduced:
            w = analysis.strategy_weights[name]
            # 找到它与哪些策略高度相关
            corr_with = [
                p for p in high_pairs
                if p.strategy_a == name or p.strategy_b == name
            ]
            if corr_with:
                partner = corr_with[0].strategy_b if corr_with[0].strategy_a == name else corr_with[0].strategy_a
                lines.append(
                    f"    {name} → {w:.2f} (correlated with {partner}, r={corr_with[0].correlation:.3f})"
                )
            else:
                lines.append(f"    {name} → {w:.2f}")
        lines.append(f"\n  Apply via: signal.ini [voting_group.<name>.weights]")

    return "\n".join(lines)


def main() -> None:
    from src.config.instance_context import set_current_environment

    parser = argparse.ArgumentParser(description="Strategy correlation analysis")
    parser.add_argument(
        "--environment",
        choices=["live", "demo"],
        required=True,
        help="显式指定分析使用哪个环境数据库",
    )
    parser.add_argument(
        "--tf", required=True, help="Timeframe(s), comma-separated"
    )
    parser.add_argument("--start", default="2025-12-30")
    parser.add_argument("--end", default="2026-03-30")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.70,
        help="Correlation threshold for 'high' (default 0.70)",
    )
    parser.add_argument(
        "--penalty",
        type=float,
        default=0.50,
        help="Weight penalty for correlated strategies (default 0.50)",
    )
    args = parser.parse_args()
    set_current_environment(args.environment)

    from src.backtesting.analysis.correlation import analyze_strategy_correlation

    timeframes = [t.strip().upper() for t in args.tf.split(",")]

    for tf in timeframes:
        sys.stderr.write(f"Running {tf} backtest to collect signal directions...\n")
        sys.stderr.flush()

        directions, result = _collect_signal_directions(tf, args.start, args.end)

        if not directions:
            print(f"\n{tf}: No signal evaluations found. Skipping.")
            continue

        # 过滤掉几乎没有信号的策略（至少 10 个非零信号）
        active_directions = {
            name: dirs
            for name, dirs in directions.items()
            if sum(1 for d in dirs if d != 0) >= 10
        }

        if len(active_directions) < 2:
            print(f"\n{tf}: Only {len(active_directions)} active strategies. Need at least 2.")
            continue

        sys.stderr.write(
            f"  {len(active_directions)} active strategies, {len(next(iter(active_directions.values())))} bars\n"
        )

        analysis = analyze_strategy_correlation(
            active_directions,
            correlation_threshold=args.threshold,
            penalty_weight=args.penalty,
        )

        n_bars = len(next(iter(active_directions.values())))
        print(_render_correlation_result(analysis, tf, n_bars))

    sys.stderr.write("Done.\n")


if __name__ == "__main__":
    main()
