"""walk_forward orchestration service tests.

把窗口切分、规则聚合、稳定性筛选从 CLI 收口到 src/research/orchestration/walk_forward.py
后，本测试族锁定语义。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.research.orchestration.walk_forward import (
    aggregate_rules_across_windows,
    split_windows,
    stable_rules,
)


def _condition(indicator: str, field: str, operator: str, threshold: float):
    return SimpleNamespace(
        indicator=indicator,
        field=field,
        operator=operator,
        threshold=threshold,
    )


def _rule(direction: str, indicator: str, threshold: float, hit_rate: float):
    return SimpleNamespace(
        direction=direction,
        conditions=[_condition(indicator, "value", ">=", threshold)],
        train_hit_rate=hit_rate,
        test_hit_rate=hit_rate - 0.02,
        train_n_samples=80,
        barrier_stats_train=[],
    )


def test_split_windows_creates_contiguous_non_overlapping_windows() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 7, tzinfo=timezone.utc)

    windows = split_windows(start, end, 3)

    assert len(windows) == 3
    assert windows[0][0] == start
    assert windows[-1][1] == end
    assert windows[0][1] == windows[1][0]
    assert windows[1][1] == windows[2][0]


def test_split_windows_rejects_invalid_input() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 7, tzinfo=timezone.utc)

    import pytest

    with pytest.raises(ValueError):
        split_windows(start, end, 1)

    with pytest.raises(ValueError):
        split_windows(end, start, 3)  # end before start


def test_aggregate_rules_groups_by_condition_key() -> None:
    rules = [
        [_rule("long", "adx14", 25.0, 0.58)],
        [_rule("long", "adx14", 25.0, 0.56), _rule("long", "rsi14", 50.0, 0.57)],
        [_rule("long", "adx14", 25.0, 0.59)],
    ]

    aggregated = aggregate_rules_across_windows(rules)

    assert "adx14.value>=25.00" in aggregated
    assert aggregated["adx14.value>=25.00"]["appearances"] == [0, 1, 2]
    assert "rsi14.value>=50.00" in aggregated


def test_stable_rules_require_minimum_window_appearances() -> None:
    per_window = [
        [_rule("long", "adx14", 25.0, 0.58)],
        [_rule("long", "adx14", 25.0, 0.56)],
        [_rule("long", "rsi14", 50.0, 0.57)],
        [_rule("long", "adx14", 25.0, 0.59)],
    ]

    aggregated = aggregate_rules_across_windows(per_window)
    stable = stable_rules(aggregated, n_splits=4, min_consistency=0.60)

    keys = [item["key"] for item in stable]
    assert keys == ["adx14.value>=25.00"]
    assert stable[0]["appearances"] == [0, 1, 3]
    assert stable[0]["appearance_count"] == 3


def test_stable_rules_sort_by_appearance_then_test_hit_rate() -> None:
    per_window = [
        [
            _rule("long", "adx14", 25.0, 0.58),
            _rule("long", "rsi14", 50.0, 0.55),
        ],
        [
            _rule("long", "adx14", 25.0, 0.59),
            _rule("long", "rsi14", 50.0, 0.56),
        ],
    ]

    aggregated = aggregate_rules_across_windows(per_window)
    stable = stable_rules(aggregated, n_splits=2, min_consistency=0.50)

    # 同 appearance_count 时按 avg_test_hit_rate desc 排序
    keys = [item["key"] for item in stable]
    assert keys[0] == "adx14.value>=25.00"
    assert keys[1] == "rsi14.value>=50.00"


def test_run_mining_walk_forward_orchestrates_via_callable() -> None:
    """run_mining_walk_forward 通过注入 callable 实现依赖反转，便于测试。"""
    from src.research.orchestration.walk_forward import run_mining_walk_forward

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 4, tzinfo=timezone.utc)

    captured_calls: list = []

    def fake_mine_window(window_start, window_end):
        captured_calls.append((window_start, window_end))
        return SimpleNamespace(
            mined_rules=[_rule("long", "adx14", 25.0, 0.58)],
        )

    result = run_mining_walk_forward(
        timeframe="H1",
        start=start,
        end=end,
        splits=3,
        min_consistency=0.60,
        mine_window=fake_mine_window,
    )

    assert result.timeframe == "H1"
    assert len(result.windows) == 3
    assert len(captured_calls) == 3
    assert result.windows[0].rule_count == 1
    # 同一规则在 3/3 窗口出现 → stable
    assert len(result.stable) == 1
    assert result.stable[0]["appearance_count"] == 3


def test_walk_forward_result_to_dict_serializable() -> None:
    from src.research.orchestration.walk_forward import (
        MiningWalkForwardResult,
        MiningWalkForwardWindow,
    )

    window = MiningWalkForwardWindow(
        index=0,
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 1, 2, tzinfo=timezone.utc),
        rule_count=2,
    )
    result = MiningWalkForwardResult(
        timeframe="H1",
        windows=[window],
        stable=[{"key": "adx14.value>=25.00", "direction": "long"}],
    )

    payload = result.to_dict()
    assert payload["timeframe"] == "H1"
    assert payload["windows"][0]["rule_count"] == 2
    assert payload["windows"][0]["start"].endswith("+00:00")
    assert payload["stable_rules"][0]["key"] == "adx14.value>=25.00"
