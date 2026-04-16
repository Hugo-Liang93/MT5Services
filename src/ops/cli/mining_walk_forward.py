"""A-3: 挖掘层 Walk-Forward 稳定性验证。

区别于 `walkforward_runner.py`（回测层 IS/OOS 评估）：本工具在挖掘阶段把时间
范围切成 N 个连续非重叠窗口，**每个窗口独立调用 mine_rules**，然后统计每条
规则（按 condition key）的跨窗口出现次数 + 平均 train/test hit rate。

目的：
    解决 squeeze_breakout_buy 式失败——单一 70/30 切分下训练期偶发规律被选中，
    但在其它时段不成立。Walk-Forward 挖掘强制要求规则在 ≥ min_consistency × N
    个窗口中出现且保持 hit rate 门槛才算稳健候选，直接杜绝样本波动造成的过拟合。

用法：
    python -m src.ops.cli.mining_walk_forward \\
        --environment live --tf H1 \\
        --start 2024-04-01 --end 2026-03-30 --splits 6

输出：
    - 每个窗口的 mined rules 数量
    - Stable Rules 清单（至少在 M 个窗口出现）+ 各窗口 hit rate
    - 平均 barrier_stats（汇总 top-1 barrier 组合的稳定性）
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import warnings

warnings.filterwarnings("ignore")

import logging

logging.disable(logging.CRITICAL)

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple


def _split_windows(
    start: datetime, end: datetime, n_splits: int
) -> List[Tuple[datetime, datetime]]:
    """把 [start, end] 切成 N 个连续非重叠窗口。"""
    if n_splits < 2:
        raise ValueError(f"n_splits must be >= 2, got {n_splits}")
    total_seconds = (end - start).total_seconds()
    window_seconds = total_seconds / n_splits
    windows: List[Tuple[datetime, datetime]] = []
    for i in range(n_splits):
        w_start = start + timedelta(seconds=i * window_seconds)
        w_end = start + timedelta(seconds=(i + 1) * window_seconds)
        windows.append((w_start, w_end))
    return windows


def _mine_window(
    tf: str,
    window_start: datetime,
    window_end: datetime,
) -> Any:
    """在单个窗口跑挖掘，返回 MiningResult（含 mined_rules）。"""
    from src.backtesting.component_factory import build_backtest_components
    from src.research.core import load_research_config
    from src.research.orchestration import MiningRunner

    config = load_research_config()
    components = build_backtest_components()
    runner = MiningRunner(config=config, components=components)

    return runner.run(
        symbol="XAUUSD",
        timeframe=tf,
        start_time=window_start,
        end_time=window_end,
    )


def _rule_key(conditions: list) -> str:
    """将规则条件列表序列化为 key（跨窗口对齐用）。

    复用 rule_mining._rule_condition_key 的逻辑：对 condition 按 (indicator, field,
    operator) 排序，threshold 做离散化（两位有效数字）避免窗口间数值微差。
    """
    parts: List[str] = []
    for c in sorted(
        conditions, key=lambda x: (x.indicator, x.field, x.operator)
    ):
        parts.append(f"{c.indicator}.{c.field}{c.operator}{c.threshold:.2f}")
    return " & ".join(parts)


def _aggregate_rules_across_windows(
    per_window_rules: List[List[Any]],
) -> Dict[str, Dict[str, Any]]:
    """聚合跨窗口规则。

    Returns:
        {rule_key: {
            "direction": ...,
            "appearances": [window_idx, ...],
            "train_hit_rates": [...],
            "test_hit_rates": [...],
            "train_n_samples": [...],
            "barrier_top_hit_rates": [...],  # 每窗口 barrier_stats_train[0].hit_rate
            "conditions_example": ...,  # 第一次出现时的完整条件（用于展示）
        }}
    """
    aggregated: Dict[str, Dict[str, Any]] = {}

    for window_idx, rules in enumerate(per_window_rules):
        for rule in rules:
            key = _rule_key(rule.conditions)
            if key not in aggregated:
                aggregated[key] = {
                    "direction": rule.direction,
                    "appearances": [],
                    "train_hit_rates": [],
                    "test_hit_rates": [],
                    "train_n_samples": [],
                    "barrier_top_hit_rates": [],
                    "conditions_example": rule.conditions,
                }
            agg = aggregated[key]
            agg["appearances"].append(window_idx)
            agg["train_hit_rates"].append(float(rule.train_hit_rate))
            if rule.test_hit_rate is not None:
                agg["test_hit_rates"].append(float(rule.test_hit_rate))
            agg["train_n_samples"].append(int(rule.train_n_samples))
            # F-12d barrier_stats 可能为空（老 matrix 或样本少）
            if rule.barrier_stats_train:
                agg["barrier_top_hit_rates"].append(
                    float(rule.barrier_stats_train[0].hit_rate)
                )

    return aggregated


def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _render(
    tf: str,
    windows: List[Tuple[datetime, datetime]],
    per_window_rules: List[List[Any]],
    aggregated: Dict[str, Dict[str, Any]],
    min_consistency: float,
) -> str:
    n_splits = len(windows)
    min_appearances = max(2, math.ceil(n_splits * min_consistency))

    lines: List[str] = []
    lines.append("\n" + "=" * 80)
    lines.append(
        f" XAUUSD/{tf} Walk-Forward Mining ({n_splits} splits, "
        f"min_consistency={min_consistency:.0%} → min_appear={min_appearances})"
    )
    lines.append("=" * 80)

    # 每窗口摘要
    lines.append("\n--- Per-Window Summary ---")
    for i, (ws, we) in enumerate(windows):
        rules = per_window_rules[i]
        lines.append(
            f"  Window {i+1}: {ws.date()} ~ {we.date()} | {len(rules)} rules mined"
        )

    # 稳健规则
    stable = [
        (k, v)
        for k, v in aggregated.items()
        if len(v["appearances"]) >= min_appearances
    ]
    stable.sort(
        key=lambda kv: (
            -len(kv[1]["appearances"]),
            -_mean(kv[1]["train_hit_rates"]),
        )
    )

    lines.append(
        f"\n--- Stable Rules ({len(stable)} appear in ≥ {min_appearances}/{n_splits} windows) ---"
    )
    if not stable:
        lines.append(
            "  无稳健规则。所有挖出的规则均为单窗口样本波动，不应进 Paper。"
        )
        lines.append("")
        return "\n".join(lines)

    for key, agg in stable:
        app_count = len(agg["appearances"])
        train_avg = _mean(agg["train_hit_rates"])
        test_avg = _mean(agg["test_hit_rates"])
        barrier_avg = _mean(agg["barrier_top_hit_rates"])
        n_avg = _mean([float(n) for n in agg["train_n_samples"]])

        lines.append("")
        lines.append(
            f"  [{agg['direction'].upper()}] appears {app_count}/{n_splits} "
            f"| train_avg={train_avg*100:.1f}% "
            f"| test_avg={test_avg*100:.1f}% "
            f"| barrier_avg={barrier_avg*100:.1f}% "
            f"| avg_n={n_avg:.0f}"
        )
        # 展示规则条件
        conds = agg["conditions_example"]
        cond_str = " AND ".join(
            f"{c.indicator}.{c.field}{c.operator}{c.threshold:.2f}" for c in conds
        )
        lines.append(f"    Rule: IF {cond_str} THEN {agg['direction']}")
        # 各窗口 train hit 序列
        hit_str = ", ".join(
            f"w{i+1}={r*100:.0f}%"
            for i, r in zip(agg["appearances"], agg["train_hit_rates"])
        )
        lines.append(f"    Train hits: {hit_str}")

    lines.append("")
    return "\n".join(lines)


def main() -> None:
    from src.config.instance_context import set_current_environment

    parser = argparse.ArgumentParser(
        description="Mining-layer walk-forward stability validation"
    )
    parser.add_argument(
        "--environment", choices=["live", "demo"], required=True
    )
    parser.add_argument("--tf", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument(
        "--splits", type=int, default=6,
        help="窗口数（默认 6）；连续非重叠切分",
    )
    parser.add_argument(
        "--min-consistency", type=float, default=0.60,
        help="稳健阈值：规则至少在该比例的窗口出现（默认 0.60）",
    )
    args = parser.parse_args()
    set_current_environment(args.environment)

    start_dt = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end_dt = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    windows = _split_windows(start_dt, end_dt, args.splits)

    print(
        f"Splitting {args.start} ~ {args.end} into {args.splits} windows "
        f"({(end_dt - start_dt).days / args.splits:.1f} days each)",
        flush=True,
    )

    per_window_rules: List[List[Any]] = []
    for i, (ws, we) in enumerate(windows):
        print(f"  Window {i+1}/{args.splits}: {ws.date()} ~ {we.date()} ...", flush=True)
        try:
            result = _mine_window(args.tf, ws, we)
            per_window_rules.append(list(result.mined_rules or []))
        except Exception as exc:
            print(f"    FAILED: {exc}", flush=True)
            per_window_rules.append([])

    aggregated = _aggregate_rules_across_windows(per_window_rules)
    print(_render(args.tf, windows, per_window_rules, aggregated, args.min_consistency))


if __name__ == "__main__":
    main()
