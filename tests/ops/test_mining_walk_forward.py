"""A-3: mining_walk_forward CLI 纯函数测试。

不涉及挖掘流程（那是 MiningRunner 职责），只验证：
- _split_windows 切分正确性
- _rule_key 稳定排序（condition 顺序不影响 key）
- _aggregate_rules_across_windows 跨窗口聚合逻辑
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from src.ops.cli.mining_walk_forward import (
    _aggregate_rules_across_windows,
    _mine_window,
    _rule_key,
    _split_windows,
)


def test_split_windows_equal_length():
    s = datetime(2024, 1, 1, tzinfo=timezone.utc)
    e = datetime(2024, 7, 1, tzinfo=timezone.utc)
    windows = _split_windows(s, e, 3)
    assert len(windows) == 3
    # 首尾对齐
    assert windows[0][0] == s
    assert windows[-1][1] == e
    # 连续无间隙
    for i in range(len(windows) - 1):
        assert windows[i][1] == windows[i + 1][0]


def test_split_windows_rejects_invalid():
    s = datetime(2024, 1, 1, tzinfo=timezone.utc)
    e = datetime(2024, 2, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        _split_windows(s, e, 1)


def test_rule_key_stable_under_condition_reorder():
    c1 = NS(indicator="adx14", field="adx", operator="<=", threshold=10.84)
    c2 = NS(indicator="di_spread14", field="di_spread", operator=">", threshold=-0.44)
    assert _rule_key([c1, c2]) == _rule_key([c2, c1])


def test_rule_key_discriminates_threshold():
    c1 = NS(indicator="adx14", field="adx", operator="<=", threshold=10.84)
    c2 = NS(indicator="adx14", field="adx", operator="<=", threshold=15.00)
    assert _rule_key([c1]) != _rule_key([c2])


def _mock_rule(direction, conditions, train_hr, test_hr, n, barrier_hr=None):
    barrier_stats = ()
    if barrier_hr is not None:
        barrier_stats = (NS(hit_rate=barrier_hr),)
    return NS(
        direction=direction,
        conditions=conditions,
        train_hit_rate=train_hr,
        test_hit_rate=test_hr,
        train_n_samples=n,
        barrier_stats_train=barrier_stats,
    )


def test_aggregate_single_rule_across_all_windows():
    cond = NS(indicator="rsi14", field="rsi", operator="<=", threshold=30.0)
    per_window = [
        [_mock_rule("buy", [cond], 0.70, 0.65, 100, 0.60)],
        [_mock_rule("buy", [cond], 0.72, 0.60, 120, 0.58)],
        [_mock_rule("buy", [cond], 0.68, 0.55, 90, 0.55)],
    ]
    agg = _aggregate_rules_across_windows(per_window)
    assert len(agg) == 1
    key = _rule_key([cond])
    info = agg[key]
    assert info["direction"] == "buy"
    assert info["appearances"] == [0, 1, 2]
    assert info["train_hit_rates"] == [0.70, 0.72, 0.68]
    assert info["test_hit_rates"] == [0.65, 0.60, 0.55]
    assert info["train_n_samples"] == [100, 120, 90]
    assert info["barrier_top_hit_rates"] == [0.60, 0.58, 0.55]


def test_aggregate_rule_only_in_some_windows():
    c1 = NS(indicator="rsi14", field="rsi", operator="<=", threshold=30.0)
    c2 = NS(indicator="adx14", field="adx", operator=">", threshold=25.0)
    per_window = [
        [_mock_rule("buy", [c1], 0.70, 0.65, 100), _mock_rule("sell", [c2], 0.55, 0.50, 80)],
        [_mock_rule("buy", [c1], 0.72, 0.60, 120)],  # c2 不出现
        [_mock_rule("sell", [c2], 0.56, 0.52, 85)],  # c1 不出现
    ]
    agg = _aggregate_rules_across_windows(per_window)
    k1 = _rule_key([c1])
    k2 = _rule_key([c2])
    assert len(agg[k1]["appearances"]) == 2
    assert agg[k1]["appearances"] == [0, 1]
    assert len(agg[k2]["appearances"]) == 2
    assert agg[k2]["appearances"] == [0, 2]


def test_aggregate_handles_missing_test_and_barrier():
    cond = NS(indicator="macd", field="hist", operator=">", threshold=-3.0)
    # 某些 rule 可能没 test（老 matrix）或没 barrier_stats
    per_window = [
        [_mock_rule("sell", [cond], 0.60, None, 50, None)],
        [_mock_rule("sell", [cond], 0.58, 0.55, 60, 0.52)],
    ]
    agg = _aggregate_rules_across_windows(per_window)
    key = _rule_key([cond])
    info = agg[key]
    # 未 None 的 test 只进了 1 个
    assert info["test_hit_rates"] == [0.55]
    # barrier 同理
    assert info["barrier_top_hit_rates"] == [0.52]
    # train 不应受缺失影响
    assert info["train_hit_rates"] == [0.60, 0.58]


# ── §0di P2: cleanup sentinel ──


def test_mine_window_uses_with_build_research_data_deps() -> None:
    """§0di P2 sentinel：_mine_window 必须用 with 块包裹 build_research_data_deps()，
    确保 writer 连接池 + indicator pipeline 线程池在每个窗口退出前 cleanup。
    walk-forward 按 split 多次调用本函数，无 cleanup 时泄漏按窗口数线性累积。
    """
    src = inspect.getsource(_mine_window)
    assert "with build_research_data_deps()" in src, (
        "_mine_window 必须用 with build_research_data_deps() 包裹（§0di P2）；"
        f"当前实现:\n{src}"
    )
    assert "with " in src.split("build_research_data_deps()")[0].splitlines()[-1], (
        "build_research_data_deps() 必须在 with 语句中调用，而不是裸赋值"
    )
