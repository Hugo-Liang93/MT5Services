"""Triple-Barrier forward_return 核验（F-12a）。

包含：
- BarrierConfig 验证
- LONG / SHORT 各方向的 TP/SL/Time 三条 barrier 命中判定
- round_trip_cost 扣除
- ATR 缺失时的优雅 None
- 默认 configs 稳定性
"""

from __future__ import annotations

import math

import pytest

from src.research.core.barrier import (
    DEFAULT_BARRIER_CONFIGS,
    BarrierConfig,
    BarrierOutcome,
    compute_barrier_returns,
)


def _atr_snap(atr: float | None) -> dict:
    if atr is None:
        return {}
    return {"atr14": {"atr": atr}}


def test_barrier_config_rejects_invalid_values():
    with pytest.raises(ValueError):
        BarrierConfig(sl_atr=0, tp_atr=2.0, time_bars=20)
    with pytest.raises(ValueError):
        BarrierConfig(sl_atr=1.0, tp_atr=-1.0, time_bars=20)
    with pytest.raises(ValueError):
        BarrierConfig(sl_atr=1.0, tp_atr=2.0, time_bars=0)


def test_barrier_config_key_is_hashable():
    cfg = BarrierConfig(sl_atr=1.0, tp_atr=2.0, time_bars=20)
    d = {cfg.key(): "ok"}
    assert d[(1.0, 2.0, 20)] == "ok"


def test_default_barrier_configs_valid():
    assert len(DEFAULT_BARRIER_CONFIGS) >= 5
    keys = {cfg.key() for cfg in DEFAULT_BARRIER_CONFIGS}
    # 所有 key 必须唯一，否则下游字典会丢数据
    assert len(keys) == len(DEFAULT_BARRIER_CONFIGS)


def test_long_tp_hit():
    # 入场 100，ATR=2，SL=1.5×ATR=3 → 97，TP=2×ATR=4 → 104
    # bar 2 (entry+1) high=105 命中 TP
    opens = [100.0, 100.0, 103.0, 104.0]
    highs = [101.0, 101.0, 105.0, 106.0]
    lows = [99.0, 99.0, 102.0, 103.0]
    closes = [100.0, 100.0, 104.0, 105.0]
    indicators = [_atr_snap(2.0)] * 4
    cfg = BarrierConfig(sl_atr=1.5, tp_atr=2.0, time_bars=20)

    result = compute_barrier_returns(
        opens, highs, lows, closes, indicators,
        configs=(cfg,), direction="long",
    )

    outcomes = result[cfg.key()]
    # i=0: 入场价 = opens[1] = 100（bar 1 开盘入场）
    # 从 bar 2 开始扫描 high/low，bar 2 high=105 >= TP=104 命中
    # bars_held = 入场后经过的 bar 数 = 1（即 bar 1 过后，bar 2 命中）
    assert outcomes[0] is not None
    assert outcomes[0].barrier == "tp"
    assert outcomes[0].bars_held == 1
    # TP return = (104 - 100) / 100 = 0.04
    assert math.isclose(outcomes[0].return_pct, 0.04, abs_tol=1e-6)


def test_long_sl_hit():
    # 入场 100，SL=1×ATR=1 → 99；bar 2 low=98 命中 SL
    opens = [100.0, 100.0, 99.5, 99.0]
    highs = [101.0, 101.0, 100.0, 99.5]
    lows = [99.0, 99.5, 98.0, 97.0]
    closes = [100.0, 100.0, 98.5, 97.5]
    indicators = [_atr_snap(1.0)] * 4
    cfg = BarrierConfig(sl_atr=1.0, tp_atr=3.0, time_bars=20)

    result = compute_barrier_returns(
        opens, highs, lows, closes, indicators,
        configs=(cfg,), direction="long",
    )

    outcome = result[cfg.key()][0]
    assert outcome is not None
    assert outcome.barrier == "sl"
    # SL 价 = 100 - 1×1 = 99；return = (99 - 100)/100 = -0.01
    assert math.isclose(outcome.return_pct, -0.01, abs_tol=1e-6)


def test_long_time_barrier_exit():
    # 价格平稳无触碰：走满 time_bars=3，按 closes[3] 退出
    opens = [100.0, 100.0, 100.3, 100.1, 99.9]
    highs = [100.5, 100.5, 100.5, 100.2, 100.0]
    lows = [99.5, 99.8, 100.0, 99.9, 99.7]
    closes = [100.0, 100.2, 100.1, 100.0, 99.8]
    indicators = [_atr_snap(5.0)] * 5  # ATR 足够大，SL/TP 远离不会命中
    cfg = BarrierConfig(sl_atr=1.0, tp_atr=1.0, time_bars=3)

    result = compute_barrier_returns(
        opens, highs, lows, closes, indicators,
        configs=(cfg,), direction="long",
    )

    outcome = result[cfg.key()][0]
    assert outcome is not None
    assert outcome.barrier == "time"
    assert outcome.bars_held == 3
    # 入场 opens[1]=100；time exit 在 index 1+3=4 的 close=99.8
    assert math.isclose(outcome.return_pct, (99.8 - 100.0) / 100.0, abs_tol=1e-6)


def test_short_tp_hit_mirrors_long_sl():
    # SHORT: 入场 100, TP=2×ATR=2 → 98（价跌到 98 赚）
    opens = [100.0, 100.0, 99.0, 98.0]
    highs = [101.0, 101.0, 100.0, 99.0]
    lows = [99.0, 99.5, 97.5, 97.0]  # bar 2 low=97.5 < 98 → TP 命中
    closes = [100.0, 100.0, 98.5, 97.5]
    indicators = [_atr_snap(1.0)] * 4
    cfg = BarrierConfig(sl_atr=2.0, tp_atr=2.0, time_bars=20)

    result = compute_barrier_returns(
        opens, highs, lows, closes, indicators,
        configs=(cfg,), direction="short",
    )

    outcome = result[cfg.key()][0]
    assert outcome is not None
    assert outcome.barrier == "tp"
    # Short TP return = (entry - tp_price) / entry = (100 - 98)/100 = 0.02
    assert math.isclose(outcome.return_pct, 0.02, abs_tol=1e-6)


def test_round_trip_cost_deducted():
    opens = [100.0, 100.0, 103.0, 104.0]
    highs = [101.0, 101.0, 105.0, 106.0]
    lows = [99.0, 99.0, 102.0, 103.0]
    closes = [100.0, 100.0, 104.0, 105.0]
    indicators = [_atr_snap(2.0)] * 4
    cfg = BarrierConfig(sl_atr=1.5, tp_atr=2.0, time_bars=20)

    result = compute_barrier_returns(
        opens, highs, lows, closes, indicators,
        configs=(cfg,), direction="long",
        round_trip_cost_pct=0.5,  # 0.5% 成本
    )

    outcome = result[cfg.key()][0]
    # TP gross = 0.04，扣除 0.005 = 0.035
    assert math.isclose(outcome.return_pct, 0.035, abs_tol=1e-6)


def test_missing_atr_yields_none():
    opens = [100.0, 100.0, 103.0, 104.0]
    highs = [101.0, 101.0, 105.0, 106.0]
    lows = [99.0, 99.0, 102.0, 103.0]
    closes = [100.0, 100.0, 104.0, 105.0]
    # 第一根 bar 无 ATR → 该入场点 outcome = None
    indicators: list[dict] = [_atr_snap(None)] + [_atr_snap(2.0)] * 3
    cfg = BarrierConfig(sl_atr=1.5, tp_atr=2.0, time_bars=20)

    result = compute_barrier_returns(
        opens, highs, lows, closes, indicators,
        configs=(cfg,), direction="long",
    )

    outcomes = result[cfg.key()]
    # i=0 的 ATR 缺失 → None；i=1 的 ATR 有值但 i+1 还在范围内 → 可能有 outcome
    assert outcomes[0] is None


def test_insufficient_tail_returns_none():
    # 入场点在最后 → 没有未来 bar 可模拟
    opens = [100.0, 100.0]
    highs = [101.0, 101.0]
    lows = [99.0, 99.0]
    closes = [100.0, 100.0]
    indicators = [_atr_snap(2.0)] * 2
    cfg = BarrierConfig(sl_atr=1.5, tp_atr=2.0, time_bars=5)

    result = compute_barrier_returns(
        opens, highs, lows, closes, indicators,
        configs=(cfg,), direction="long",
    )

    # i=0 入场 bar 是 opens[1]，之后没有 bar → None
    assert result[cfg.key()][0] is None


def test_invalid_direction_raises():
    opens = [100.0, 100.0]
    with pytest.raises(ValueError):
        compute_barrier_returns(
            opens, opens, opens, opens, [_atr_snap(1.0)] * 2,
            direction="sideways",
        )


def test_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        compute_barrier_returns(
            [100.0, 100.0],
            [101.0, 101.0, 102.0],  # 长度不一致
            [99.0, 99.0],
            [100.0, 100.0],
            [_atr_snap(1.0)] * 2,
        )
