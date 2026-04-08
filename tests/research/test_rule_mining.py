"""Rule Mining 分析器单元测试 — 用合成数据验证决策树规则提取。"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest

from src.research.analyzers.rule_mining import MinedRule, RuleMiningConfig, mine_rules
from src.research.config import OverfittingConfig
from src.research.data_matrix import DataMatrix
from src.signals.evaluation.regime import RegimeType


def _make_rule_matrix(
    n_bars: int = 600,
    seed: int = 42,
) -> DataMatrix:
    """构造有明确规则的合成 DataMatrix。

    规则：当 rsi <= 30 AND adx > 25 时，未来 5-bar 收益为正（70% 概率）。
    其他情况随机（约 50%）。
    """
    rng = random.Random(seed)

    closes = [2000.0 + rng.gauss(0, 5) for _ in range(n_bars)]
    bar_times = [datetime(2026, 1, 1, tzinfo=timezone.utc)] * n_bars

    # 指标值
    rsi_vals = [rng.uniform(15, 85) for _ in range(n_bars)]
    adx_vals = [rng.uniform(10, 60) for _ in range(n_bars)]
    atr_vals = [rng.uniform(5, 50) for _ in range(n_bars)]

    # 前瞻收益：rsi <= 30 AND adx > 25 → 高概率正收益
    forward_vals: List[Optional[float]] = []
    for i in range(n_bars):
        if i >= n_bars - 5:
            forward_vals.append(None)
            continue
        if rsi_vals[i] <= 30 and adx_vals[i] > 25:
            # 规则区：70% 正收益
            if rng.random() < 0.70:
                forward_vals.append(abs(rng.gauss(0.003, 0.002)))
            else:
                forward_vals.append(-abs(rng.gauss(0.002, 0.001)))
        else:
            # 其他区：约 50% 随机
            forward_vals.append(rng.gauss(0.0, 0.003))

    regimes = [RegimeType.TRENDING] * n_bars
    indicators: List[Dict[str, Dict[str, Any]]] = [
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

    return DataMatrix(
        symbol="XAUUSD",
        timeframe="H1",
        n_bars=n_bars,
        bar_times=bar_times,
        opens=closes,
        highs=closes,
        lows=closes,
        closes=closes,
        volumes=[100.0] * n_bars,
        indicators=indicators,
        regimes=regimes,
        soft_regimes=[None] * n_bars,
        forward_returns={5: forward_vals},
        indicator_series=indicator_series,
        train_end_idx=train_end_idx,
        split_idx=split_idx,
    )


class TestRuleMining:
    def test_discovers_embedded_rule(self) -> None:
        """应发现 rsi <= ~30 AND adx > ~25 的买入规则。"""
        matrix = _make_rule_matrix(800, seed=42)

        rules = mine_rules(
            matrix,
            horizons=[5],
            config=RuleMiningConfig(
                max_depth=3,
                min_samples_leaf=15,
                min_hit_rate=0.55,
                min_test_hit_rate=0.50,
            ),
            overfitting_config=OverfittingConfig(min_samples=15),
        )

        assert len(rules) > 0

        # 至少有一条 buy 规则
        buy_rules = [r for r in rules if r.direction == "buy"]
        assert len(buy_rules) > 0

        # 最佳规则应涉及 rsi 或 adx
        best = buy_rules[0]
        indicators_used = {c.indicator for c in best.conditions}
        assert "rsi14" in indicators_used or "adx14" in indicators_used

        # 训练集命中率应 > 55%
        assert best.train_hit_rate > 0.55

    def test_train_test_validation(self) -> None:
        """规则应同时有训练集和测试集结果。"""
        matrix = _make_rule_matrix(600, seed=123)

        rules = mine_rules(
            matrix,
            horizons=[5],
            config=RuleMiningConfig(
                max_depth=2,
                min_samples_leaf=10,
                min_hit_rate=0.52,
                min_test_hit_rate=0.45,
            ),
            overfitting_config=OverfittingConfig(min_samples=10),
        )

        for rule in rules:
            assert rule.train_n_samples > 0
            assert rule.train_hit_rate > 0.0
            # 测试集应有数据
            if rule.test_hit_rate is not None:
                assert rule.test_n_samples > 0

    def test_conditions_are_interpretable(self) -> None:
        """每条规则的条件应可解释（有 indicator/field/operator/threshold）。"""
        matrix = _make_rule_matrix(600, seed=42)

        rules = mine_rules(
            matrix,
            horizons=[5],
            config=RuleMiningConfig(max_depth=3, min_samples_leaf=10, min_hit_rate=0.52, min_test_hit_rate=0.45),
            overfitting_config=OverfittingConfig(min_samples=10),
        )

        for rule in rules:
            assert len(rule.conditions) > 0
            assert len(rule.conditions) <= 3  # max_depth=3
            for cond in rule.conditions:
                assert cond.indicator in ("rsi14", "adx14", "atr14")
                assert cond.operator in ("<=", ">")
                assert isinstance(cond.threshold, float)

    def test_rule_string(self) -> None:
        """rule_string 应生成可读的 IF-THEN 表达式。"""
        matrix = _make_rule_matrix(600, seed=42)

        rules = mine_rules(
            matrix,
            horizons=[5],
            config=RuleMiningConfig(max_depth=2, min_samples_leaf=10, min_hit_rate=0.52, min_test_hit_rate=0.45),
            overfitting_config=OverfittingConfig(min_samples=10),
        )

        assert len(rules) > 0
        rs = rules[0].rule_string()
        assert rs.startswith("IF ")
        assert " THEN " in rs
        assert rules[0].direction in rs

    def test_insufficient_data(self) -> None:
        """数据不足时返回空列表。"""
        matrix = _make_rule_matrix(30, seed=42)

        rules = mine_rules(
            matrix,
            overfitting_config=OverfittingConfig(min_samples=100),
        )

        assert len(rules) == 0

    def test_to_dict_format(self) -> None:
        """验证 to_dict 输出格式。"""
        matrix = _make_rule_matrix(600, seed=42)

        rules = mine_rules(
            matrix,
            horizons=[5],
            config=RuleMiningConfig(max_depth=2, min_samples_leaf=10, min_hit_rate=0.52, min_test_hit_rate=0.45),
            overfitting_config=OverfittingConfig(min_samples=10),
        )

        assert len(rules) > 0
        d = rules[0].to_dict()
        assert "rule" in d
        assert "direction" in d
        assert "conditions" in d
        assert "train" in d
        assert "test" in d
        assert "hit_rate" in d["train"]
        assert isinstance(d["conditions"], list)

    def test_max_rules_limit(self) -> None:
        """输出规则数不超过 max_rules。"""
        matrix = _make_rule_matrix(600, seed=42)

        rules = mine_rules(
            matrix,
            horizons=[5],
            config=RuleMiningConfig(
                max_depth=3, min_samples_leaf=5,
                min_hit_rate=0.50, min_test_hit_rate=0.40,
                max_rules=3,
            ),
            overfitting_config=OverfittingConfig(min_samples=5),
        )

        assert len(rules) <= 3
