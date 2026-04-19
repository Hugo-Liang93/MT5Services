"""Snapshot-based delta metric 注入的单元测试。

验证 `src/backtesting/engine/indicators.py::_apply_delta_to_snapshots` 的纯函数语义：
- 回看 N 根 snapshot 计算 `{metric}_d{N}` 差值
- 越界（i < delta）跳过
- 非数字字段跳过
- 已含 `_dN` 的字段不再递归（避免 rsi_d3_d3）
- 多 delta 值共存（如 delta_bars=[3, 5]）
"""

from __future__ import annotations

import pytest

from src.backtesting.engine.indicators import _apply_delta_to_snapshots


def _mk_snapshot(**indicators: dict) -> dict:
    return {name: dict(payload) for name, payload in indicators.items()}


def test_apply_delta_basic_three_bar_window() -> None:
    """delta_bars=[3]，第 3 根之后开始有 d3 字段，值 = current - prev。"""
    snapshots = [
        _mk_snapshot(adx14={"adx": 20.0, "plus_di": 15.0}),
        _mk_snapshot(adx14={"adx": 22.0, "plus_di": 16.0}),
        _mk_snapshot(adx14={"adx": 25.0, "plus_di": 18.0}),
        _mk_snapshot(adx14={"adx": 28.0, "plus_di": 20.0}),  # i=3，d3 = 28-20=8
        _mk_snapshot(adx14={"adx": 30.0, "plus_di": 22.0}),  # i=4，d3 = 30-22=8
    ]
    _apply_delta_to_snapshots(snapshots, {"adx14": (3,)})

    # 前 3 根不应有 delta 字段
    for i in range(3):
        assert "adx_d3" not in snapshots[i]["adx14"]
        assert "plus_di_d3" not in snapshots[i]["adx14"]

    # i=3, i=4 应有 delta
    assert snapshots[3]["adx14"]["adx_d3"] == pytest.approx(8.0)
    assert snapshots[3]["adx14"]["plus_di_d3"] == pytest.approx(5.0)
    assert snapshots[4]["adx14"]["adx_d3"] == pytest.approx(8.0)
    assert snapshots[4]["adx14"]["plus_di_d3"] == pytest.approx(6.0)


def test_apply_delta_multiple_deltas_coexist() -> None:
    """delta_bars=[3, 5] 同一指标同时产出 d3 / d5。"""
    snapshots = [_mk_snapshot(rsi14={"rsi": float(50 + i)}) for i in range(8)]
    _apply_delta_to_snapshots(snapshots, {"rsi14": (3, 5)})

    # i=5：d3 = 55-52=3, d5 = 55-50=5
    assert snapshots[5]["rsi14"]["rsi_d3"] == pytest.approx(3.0)
    assert snapshots[5]["rsi14"]["rsi_d5"] == pytest.approx(5.0)
    # i=3: d3 present, d5 不够数据
    assert "rsi_d3" in snapshots[3]["rsi14"]
    assert "rsi_d5" not in snapshots[3]["rsi14"]


def test_apply_delta_skips_non_numeric_fields() -> None:
    """非数字字段（如 str / None / dict）跳过。"""
    snapshots = [
        _mk_snapshot(
            mixed={"num": 10.0, "label": "up", "nested": {"k": 1}, "null": None}
        ),
        _mk_snapshot(
            mixed={"num": 15.0, "label": "down", "nested": {"k": 2}, "null": None}
        ),
        _mk_snapshot(
            mixed={"num": 20.0, "label": "flat", "nested": {"k": 3}, "null": None}
        ),
    ]
    _apply_delta_to_snapshots(snapshots, {"mixed": (2,)})

    out = snapshots[2]["mixed"]
    assert out["num_d2"] == pytest.approx(10.0)
    assert "label_d2" not in out
    assert "nested_d2" not in out
    assert "null_d2" not in out


def test_apply_delta_skips_existing_delta_keys() -> None:
    """已含 _dN 后缀的字段不再递归计算 _dN_dN。"""
    snapshots = [
        _mk_snapshot(adx14={"adx": 20.0, "adx_d3": 1.0}),
        _mk_snapshot(adx14={"adx": 22.0, "adx_d3": 2.0}),
        _mk_snapshot(adx14={"adx": 25.0, "adx_d3": 3.0}),
        _mk_snapshot(adx14={"adx": 28.0, "adx_d3": 4.0}),
    ]
    _apply_delta_to_snapshots(snapshots, {"adx14": (3,)})

    # adx_d3 应被更新（覆盖掉原值），但不产生 adx_d3_d3
    assert snapshots[3]["adx14"]["adx_d3"] == pytest.approx(8.0)  # 28-20=8
    assert "adx_d3_d3" not in snapshots[3]["adx14"]


def test_apply_delta_handles_missing_indicator_in_prev_snapshot() -> None:
    """前值 snapshot 中没有该 indicator 时跳过，不崩。"""
    snapshots = [
        _mk_snapshot(),  # 空 snapshot
        _mk_snapshot(rsi14={"rsi": 50.0}),
        _mk_snapshot(rsi14={"rsi": 55.0}),
        _mk_snapshot(rsi14={"rsi": 60.0}),
    ]
    _apply_delta_to_snapshots(snapshots, {"rsi14": (3,)})

    # i=3 回看 i=0，i=0 没有 rsi14 → 跳过，不报错
    assert "rsi_d3" not in snapshots[3]["rsi14"]


def test_apply_delta_handles_missing_metric_in_prev() -> None:
    """前值 indicator 中缺少某 metric 时跳过该 metric。"""
    snapshots = [
        _mk_snapshot(rsi14={"rsi": 50.0}),  # 无 rsi_d3
        _mk_snapshot(rsi14={"rsi": 52.0}),
        _mk_snapshot(rsi14={"rsi": 55.0}),
        _mk_snapshot(rsi14={"rsi": 58.0, "extra": 1.0}),  # 新加 extra 字段
    ]
    _apply_delta_to_snapshots(snapshots, {"rsi14": (3,)})

    # rsi_d3 正常
    assert snapshots[3]["rsi14"]["rsi_d3"] == pytest.approx(8.0)
    # extra 前值缺失 → 无 extra_d3
    assert "extra_d3" not in snapshots[3]["rsi14"]


def test_apply_delta_empty_config_is_noop() -> None:
    """空配置时不修改 snapshots。"""
    snapshots = [_mk_snapshot(rsi14={"rsi": 50.0})]
    before = {k: dict(v) for k, v in snapshots[0].items()}
    _apply_delta_to_snapshots(snapshots, {})
    assert snapshots[0] == before


def test_apply_delta_empty_snapshots_is_noop() -> None:
    """空快照列表不崩。"""
    snapshots: list = []
    _apply_delta_to_snapshots(snapshots, {"rsi14": (3,)})
    assert snapshots == []


def test_apply_delta_skips_non_dict_payload() -> None:
    """pipeline per-indicator 失败时 payload 可能是 None 或非 dict，必须跳过不崩。

    回归：之前的实现假设 payload 永远是 dict，一次 indicator 失败会
    AttributeError 炸掉整个 precompute 步骤。
    """
    snapshots = [
        _mk_snapshot(rsi14={"rsi": 50.0}, adx14={"adx": 20.0}),
        # 第 2 根 adx14 payload 是 None（模拟 pipeline 失败）
        {"rsi14": {"rsi": 52.0}, "adx14": None},
        _mk_snapshot(rsi14={"rsi": 55.0}, adx14={"adx": 25.0}),
        _mk_snapshot(rsi14={"rsi": 58.0}, adx14={"adx": 28.0}),
    ]

    # 不应抛异常
    _apply_delta_to_snapshots(snapshots, {"rsi14": (3,), "adx14": (3,)})

    # rsi14 正常（所有 snapshot 都是 dict）
    assert snapshots[3]["rsi14"]["rsi_d3"] == pytest.approx(8.0)
    # adx14 在 i=3 回看 i=0 → 前值存在 → 应有 d3（跳过中间的 None snapshot 不影响）
    assert snapshots[3]["adx14"]["adx_d3"] == pytest.approx(8.0)
    # 中间 None payload 原样保留
    assert snapshots[1]["adx14"] is None


def test_apply_delta_skips_when_prev_payload_not_dict() -> None:
    """前值 snapshot 中该 indicator 是 None（非 dict）时跳过，不崩。"""
    snapshots = [
        {"adx14": None},  # i=0 失败
        _mk_snapshot(adx14={"adx": 22.0}),
        _mk_snapshot(adx14={"adx": 25.0}),
        _mk_snapshot(adx14={"adx": 28.0}),  # i=3 回看 i=0 → 前值非 dict
    ]
    _apply_delta_to_snapshots(snapshots, {"adx14": (3,)})
    # i=3 跳过（前值是 None），不报错
    assert "adx_d3" not in snapshots[3]["adx14"]
