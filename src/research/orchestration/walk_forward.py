"""Mining-layer walk-forward orchestration.

将 walk-forward 挖掘的窗口切分、规则聚合、稳定性筛选从 CLI 收口为可测试的
服务模块。CLI（`src/ops/cli/mining_walk_forward.py`）退化为参数适配 + I/O，
不再持有跨窗口聚合语义。

设计要点（[plan-md-radiant-sparrow.md](plan-md-radiant-sparrow.md) Task 4）：
- 纯函数 + 显式注入 `mine_window: Callable[[start, end], Result]`，
  避免 Optional 默认值 / getattr 兜底等补丁式依赖
- `split_windows` 最后一个窗口结束时间 = `end` 参数（避免浮点累加误差）
- `MiningWalkForwardResult.to_dict()` 输出 ISO-8601 + `+00:00` 后缀
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Tuple


@dataclass(frozen=True)
class MiningWalkForwardWindow:
    """单个 walk-forward 窗口的摘要。"""

    index: int
    start: datetime
    end: datetime
    rule_count: int


@dataclass(frozen=True)
class MiningWalkForwardResult:
    """walk-forward 跑批的产物。"""

    timeframe: str
    windows: List[MiningWalkForwardWindow]
    stable: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timeframe": self.timeframe,
            "windows": [
                {
                    "index": window.index,
                    "start": window.start.isoformat(),
                    "end": window.end.isoformat(),
                    "rule_count": window.rule_count,
                }
                for window in self.windows
            ],
            "stable_rules": self.stable,
        }


def split_windows(
    start: datetime, end: datetime, n_splits: int
) -> List[Tuple[datetime, datetime]]:
    """把 [start, end] 切成 N 个连续非重叠窗口。

    最后一个窗口的 end 锁定为传入的 end 参数，避免浮点累加误差让
    `windows[-1][1] != end`（CLI 旧实现的隐患）。
    """
    if n_splits < 2:
        raise ValueError(f"n_splits must be >= 2, got {n_splits}")
    total_seconds = (end - start).total_seconds()
    if total_seconds <= 0:
        raise ValueError("end must be after start")
    window_seconds = total_seconds / n_splits
    windows: List[Tuple[datetime, datetime]] = []
    for i in range(n_splits):
        w_start = start + timedelta(seconds=i * window_seconds)
        w_end = (
            end
            if i == n_splits - 1
            else start + timedelta(seconds=(i + 1) * window_seconds)
        )
        windows.append((w_start, w_end))
    return windows


def rule_key(conditions: list) -> str:
    """将规则条件列表序列化为跨窗口对齐 key。

    与 CLI 旧实现行为一致：condition 按 (indicator, field, operator) 排序，
    threshold 保留两位小数（避免窗口间数值微差）。
    """
    parts: List[str] = []
    for condition in sorted(
        conditions,
        key=lambda c: (c.indicator, c.field, c.operator),
    ):
        parts.append(
            f"{condition.indicator}.{condition.field}"
            f"{condition.operator}{condition.threshold:.2f}"
        )
    return " & ".join(parts)


def aggregate_rules_across_windows(
    per_window_rules: List[List[Any]],
) -> Dict[str, Dict[str, Any]]:
    """聚合跨窗口规则。

    Returns:
        ``{rule_key: {direction, appearances, train/test_hit_rates,
        train_n_samples, barrier_top_hit_rates, conditions_example}}``
    """
    aggregated: Dict[str, Dict[str, Any]] = {}
    for window_idx, rules in enumerate(per_window_rules):
        for rule in rules:
            key = rule_key(rule.conditions)
            if key not in aggregated:
                aggregated[key] = {
                    "key": key,
                    "direction": rule.direction,
                    "appearances": [],
                    "train_hit_rates": [],
                    "test_hit_rates": [],
                    "train_n_samples": [],
                    "barrier_top_hit_rates": [],
                    "conditions_example": list(rule.conditions),
                }
            item = aggregated[key]
            item["appearances"].append(window_idx)
            item["train_hit_rates"].append(float(rule.train_hit_rate))
            if rule.test_hit_rate is not None:
                item["test_hit_rates"].append(float(rule.test_hit_rate))
            item["train_n_samples"].append(int(rule.train_n_samples))
            barrier_train = getattr(rule, "barrier_stats_train", None) or []
            if barrier_train:
                top = barrier_train[0]
                top_hit_rate = getattr(top, "hit_rate", None)
                if top_hit_rate is not None:
                    item["barrier_top_hit_rates"].append(float(top_hit_rate))
    return aggregated


def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stable_rules(
    aggregated: Dict[str, Dict[str, Any]],
    *,
    n_splits: int,
    min_consistency: float,
) -> List[Dict[str, Any]]:
    """从聚合规则中筛出 appearance ≥ ceil(n_splits × min_consistency) 的稳定规则。

    排序：appearance_count desc → avg_test_hit_rate desc
    → avg_train_hit_rate desc。
    """
    min_appearances = max(2, math.ceil(n_splits * min_consistency))
    selected: List[Dict[str, Any]] = []
    for item in aggregated.values():
        appearance_count = len(item["appearances"])
        if appearance_count < min_appearances:
            continue
        selected.append(
            {
                "key": item["key"],
                "direction": item["direction"],
                "appearances": list(item["appearances"]),
                "appearance_count": appearance_count,
                "avg_train_hit_rate": _mean(item["train_hit_rates"]),
                "avg_test_hit_rate": _mean(item["test_hit_rates"]),
                "avg_train_n_samples": _mean(
                    [float(v) for v in item["train_n_samples"]]
                ),
                "avg_barrier_top_hit_rate": _mean(
                    item["barrier_top_hit_rates"]
                ),
            }
        )
    selected.sort(
        key=lambda item: (
            -int(item["appearance_count"]),
            -float(item["avg_test_hit_rate"]),
            -float(item["avg_train_hit_rate"]),
        )
    )
    return selected


def run_mining_walk_forward(
    *,
    timeframe: str,
    start: datetime,
    end: datetime,
    splits: int,
    min_consistency: float,
    mine_window: Callable[[datetime, datetime], Any],
) -> MiningWalkForwardResult:
    """编排 walk-forward 跑批。

    `mine_window(start, end) -> MiningResult-like` 由调用方注入，便于测试时
    用纯 SimpleNamespace 替代真实 MiningRunner。CLI 适配层注入真实实现。
    """
    windows = split_windows(start, end, splits)
    per_window_rules: List[List[Any]] = []
    summaries: List[MiningWalkForwardWindow] = []
    for idx, (window_start, window_end) in enumerate(windows):
        result = mine_window(window_start, window_end)
        rules = list(getattr(result, "mined_rules", None) or [])
        per_window_rules.append(rules)
        summaries.append(
            MiningWalkForwardWindow(
                index=idx,
                start=window_start,
                end=window_end,
                rule_count=len(rules),
            )
        )
    aggregated = aggregate_rules_across_windows(per_window_rules)
    return MiningWalkForwardResult(
        timeframe=timeframe,
        windows=summaries,
        stable=stable_rules(
            aggregated,
            n_splits=splits,
            min_consistency=min_consistency,
        ),
    )
