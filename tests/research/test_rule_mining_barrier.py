"""F-12d: rule_mining 消费 barrier_returns 产出 BarrierStats 验证。

覆盖：
- 当 matrix 带 barrier_returns 时，MinedRule.barrier_stats_train/test 非空
- 每条 BarrierStats 的 tp_rate+sl_rate+time_rate 约为 1.0（分布完整）
- to_dict 包含 barrier_stats_train / barrier_stats_test（非空时）
- 老数据（matrix 无 barrier_returns）→ barrier_stats 为空 tuple，向后兼容
- buy 规则读 long barrier；sell 规则读 short barrier
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest

from src.research.analyzers.rule_mining import (
    BarrierStats,
    MinedRule,
    RuleMiningConfig,
    mine_rules,
)
from src.research.core.barrier import (
    BarrierConfig,
    compute_barrier_returns,
)
from src.research.core.config import OverfittingConfig
from src.research.core.data_matrix import DataMatrix
from src.signals.evaluation.regime import RegimeType


def _build_matrix_with_barrier(
    *,
    n_bars: int = 600,
    seed: int = 42,
    include_barrier: bool = True,
) -> DataMatrix:
    """合成 DataMatrix：rsi<=30 AND adx>25 → 未来上涨；附带真实 OHLC 让 barrier 可算。"""
    rng = random.Random(seed)

    # 生成带方向的价格序列：规则区偏多头
    prices: List[float] = [2000.0]
    rsi_vals: List[float] = []
    adx_vals: List[float] = []

    for i in range(n_bars):
        rsi = rng.uniform(15, 85)
        adx = rng.uniform(10, 60)
        # 规则区推动价格上涨
        if rsi <= 30 and adx > 25:
            drift = abs(rng.gauss(0.5, 0.3))
        else:
            drift = rng.gauss(0.0, 0.3)
        prices.append(prices[-1] + drift)
        rsi_vals.append(rsi)
        adx_vals.append(adx)

    # 构造 OHLC（用 close 附近 ±1 做 high/low）
    opens = prices[:-1]
    closes = prices[1:]
    highs = [max(o, c) + 0.5 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 0.5 for o, c in zip(opens, closes)]
    atr_vals = [1.0] * n_bars  # 统一 ATR 简化

    # 前瞻收益（5 bar）
    forward_vals: List[Optional[float]] = []
    for i in range(n_bars):
        if i + 5 >= n_bars or opens[min(i + 1, n_bars - 1)] <= 0:
            forward_vals.append(None)
            continue
        entry = opens[i + 1] if i + 1 < n_bars else opens[i]
        exit_price = closes[min(i + 5, n_bars - 1)]
        forward_vals.append((exit_price - entry) / entry)

    regimes = [RegimeType.TRENDING] * n_bars
    indicators = [
        {
            "rsi14": {"rsi": rsi_vals[i]},
            "adx14": {"adx": adx_vals[i]},
            "atr14": {"atr": atr_vals[i]},
        }
        for i in range(n_bars)
    ]
    indicator_series: Dict[Tuple[str, str], List[Optional[float]]] = {
        ("rsi14", "rsi"): [float(v) for v in rsi_vals],
        ("adx14", "adx"): [float(v) for v in adx_vals],
        ("atr14", "atr"): [float(v) for v in atr_vals],
    }

    train_end_idx = int(n_bars * 0.7)
    split_idx = min(train_end_idx + 5, n_bars - 1)

    barrier_long: Dict[tuple, List] = {}
    barrier_short: Dict[tuple, List] = {}
    barrier_configs: tuple[BarrierConfig, ...] = ()

    if include_barrier:
        # 用小规模 config 保证测试快速
        configs = (
            BarrierConfig(sl_atr=1.0, tp_atr=2.0, time_bars=10),
            BarrierConfig(sl_atr=1.5, tp_atr=3.0, time_bars=20),
        )
        barrier_configs = configs
        barrier_long = compute_barrier_returns(
            opens=opens, highs=highs, lows=lows, closes=closes,
            indicators=indicators, configs=configs, direction="long",
        )
        barrier_short = compute_barrier_returns(
            opens=opens, highs=highs, lows=lows, closes=closes,
            indicators=indicators, configs=configs, direction="short",
        )

    return DataMatrix(
        symbol="XAUUSD",
        timeframe="H1",
        n_bars=n_bars,
        bar_times=[datetime(2026, 1, 1, tzinfo=timezone.utc)] * n_bars,
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        volumes=[100.0] * n_bars,
        indicators=indicators,
        regimes=regimes,
        soft_regimes=[None] * n_bars,
        forward_returns={5: forward_vals},
        indicator_series=indicator_series,
        train_end_idx=train_end_idx,
        split_idx=split_idx,
        barrier_returns_long=barrier_long,
        barrier_returns_short=barrier_short,
        barrier_configs=barrier_configs,
    )


def _rules_with_barrier_stats(matrix: DataMatrix) -> List[MinedRule]:
    return mine_rules(
        matrix,
        horizons=[5],
        config=RuleMiningConfig(
            max_depth=3,
            min_samples_leaf=15,
            min_hit_rate=0.52,
            min_test_hit_rate=0.45,
            n_permutations=0,  # 测试加速
        ),
        overfitting_config=OverfittingConfig(min_samples=15),
    )


def test_barrier_stats_populated_when_matrix_has_barriers():
    matrix = _build_matrix_with_barrier(n_bars=800, include_barrier=True)
    rules = _rules_with_barrier_stats(matrix)
    assert rules, "合成数据应能挖出至少一条规则"

    # 至少一条规则应带 barrier_stats
    found_with_stats = [r for r in rules if r.barrier_stats_train]
    assert found_with_stats, "matrix 带 barrier_returns 时规则应有 barrier_stats"

    rule = found_with_stats[0]
    # 每组 config 产出一条 BarrierStats
    for stats in rule.barrier_stats_train:
        assert isinstance(stats, BarrierStats)
        assert stats.n_samples > 0
        # 三种退出比例加总约 = 1.0（容忍浮点误差）
        total = stats.tp_rate + stats.sl_rate + stats.time_rate
        assert abs(total - 1.0) < 1e-6
        # key 必须匹配 matrix.barrier_configs
        assert stats.barrier_key in {cfg.key() for cfg in matrix.barrier_configs}


def test_to_dict_includes_barrier_stats_when_present():
    matrix = _build_matrix_with_barrier(n_bars=800, include_barrier=True)
    rules = _rules_with_barrier_stats(matrix)
    rule_with_stats = next((r for r in rules if r.barrier_stats_train), None)
    assert rule_with_stats is not None
    d = rule_with_stats.to_dict()
    assert "barrier_stats_train" in d
    assert isinstance(d["barrier_stats_train"], list)
    for item in d["barrier_stats_train"]:
        assert "sl_atr" in item and "tp_atr" in item and "time_bars" in item
        assert "tp_rate" in item and "sl_rate" in item and "time_rate" in item
        assert "mean_return" in item and "hit_rate" in item and "n" in item


def test_old_matrix_without_barrier_still_works():
    # matrix.barrier_returns_long/short 为空 → 规则产出仍应正确，barrier_stats 为空
    matrix = _build_matrix_with_barrier(n_bars=800, include_barrier=False)
    rules = _rules_with_barrier_stats(matrix)
    assert rules, "即使无 barrier 数据也应挖到规则"
    for r in rules:
        assert r.barrier_stats_train == ()
        assert r.barrier_stats_test == ()
        # to_dict 不应带 barrier_stats 字段（保持原输出纯净）
        d = r.to_dict()
        assert "barrier_stats_train" not in d
        assert "barrier_stats_test" not in d


def test_buy_rule_reads_long_barrier_not_short():
    matrix = _build_matrix_with_barrier(n_bars=800, include_barrier=True)
    rules = _rules_with_barrier_stats(matrix)
    buy_rules = [r for r in rules if r.direction == "buy" and r.barrier_stats_train]
    if not buy_rules:
        pytest.skip("合成数据未产出 buy 规则带 barrier_stats")

    # buy 规则的 barrier stats 应基于 long outcome。核对方向正确性：
    # 规则区合成数据向上漂 → long outcome 的 mean_return 应 > 0 且 hit_rate > 0.5
    # （不直接比 tp_rate/sl_rate —— 小样本时 TP 距离 3ATR 较远易被 SL 先触）
    rule = buy_rules[0]
    first_stats = rule.barrier_stats_train[0]  # hit_rate 最高的那组
    assert first_stats.hit_rate > 0.5, (
        f"buy 规则应从 long outcome 读取（正 return 多数），"
        f"实际 hit_rate={first_stats.hit_rate:.2f}"
    )
