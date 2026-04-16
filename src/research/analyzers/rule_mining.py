"""规则挖掘分析器 — 用决策树从数据中提取多条件交易规则。

纯数据驱动：(indicator_values) → (future_return > 0?) 的分类问题。
用 sklearn DecisionTree 拟合后，提取可解释的 IF-THEN 规则。

输出映射到 StructuredStrategyBase 的 Why/When/Where 三层框架：
  - Why（硬门控）：趋势/方向类指标 → 确定方向
  - When（硬门控）：时机/振荡类指标 → 入场时机
  - Where（软门控）：价格结构类指标 → 结构位加分

统计防护（与 Predictive Power / Threshold Sweep 对齐）：
  - 排列检验：打乱 y 后重新训练决策树，统计 null distribution
  - CV 一致性：5-fold 时序 CV，检查同一规则是否跨 fold 稳定出现
  - Binomial 检验：test set hit_rate 是否显著优于 50%
  - Train/Test 分割：标准 70/30

设计原则：
  - 树深度限制（max_depth=3~4）确保规则可解释
  - 每个叶节点最小样本数确保统计可靠
  - 仅输出 hit_rate > 50% 的"盈利方向"叶节点
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from sklearn.tree import DecisionTreeClassifier

from src.signals.evaluation.regime import RegimeType

from ..core.config import OverfittingConfig
from ..core.data_matrix import DataMatrix
from ..core.statistics import auto_block_size, binomial_test_p, block_shuffle

logger = logging.getLogger(__name__)


# ── 指标 → Why/When/Where 角色映射 ──────────────────────────────
# Why:   趋势/方向确认（ADX 强度、MA 方向、MACD 方向、Supertrend）
# When:  入场时机（RSI 极端、StochRSI、CCI、bar 形态）
# Where: 价格结构位（BB 位置、Donchian 通道、Keltner、ATR 波动率）

_WHY_INDICATORS = frozenset(
    {
        "adx14",
        "supertrend14",
        "ema50",
        "ema9",
        "sma20",
        "macd",
        "hma20",
    }
)
_WHEN_INDICATORS = frozenset(
    {
        "rsi14",
        "stoch_rsi14",
        "cci20",
        "williams_r14",
        "bar_stats20",
        "roc12",
    }
)
_WHERE_INDICATORS = frozenset(
    {
        "boll20",
        "donchian20",
        "keltner20",
        "atr14",
    }
)


def _classify_role(indicator: str) -> str:
    """根据指标名分配 Why/When/Where 角色。"""
    if indicator in _WHY_INDICATORS:
        return "why"
    if indicator in _WHEN_INDICATORS:
        return "when"
    if indicator in _WHERE_INDICATORS:
        return "where"
    return "why"  # 未知指标默认归为方向确认


@dataclass(frozen=True)
class RuleCondition:
    """规则的单个条件。"""

    indicator: str  # "rsi14"
    field: str  # "rsi"
    operator: str  # "<=" | ">"
    threshold: float  # 28.5
    role: str = ""  # "why" | "when" | "where"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "indicator": self.indicator,
            "field": self.field,
            "operator": self.operator,
            "threshold": round(self.threshold, 4),
            "role": self.role,
        }

    def __str__(self) -> str:
        return f"{self.indicator}.{self.field} {self.operator} {self.threshold:.2f}"


@dataclass(frozen=True)
class BarrierStats:
    """F-12d：单个 barrier config 下规则触发样本的退出分布统计。

    为挖掘产出提供"在某 (SL, TP, Time) 参数组合下该规则的真实胜率"，
    与朴素 forward_return 胜率并列呈现。训练/测试集分开计算。
    """

    barrier_key: Tuple[float, float, int]  # (sl_atr, tp_atr, time_bars)
    n_samples: int  # 有 outcome 的触发样本数
    tp_rate: float  # 先命中 TP 的比例
    sl_rate: float  # 先命中 SL 的比例
    time_rate: float  # 因超时退出的比例
    mean_return: float  # 平均退出 return（已扣成本）
    hit_rate: float  # return > 0 的样本比例

    def to_dict(self) -> Dict[str, Any]:
        sl, tp, time_bars = self.barrier_key
        return {
            "sl_atr": sl,
            "tp_atr": tp,
            "time_bars": time_bars,
            "n": self.n_samples,
            "tp_rate": round(self.tp_rate, 4),
            "sl_rate": round(self.sl_rate, 4),
            "time_rate": round(self.time_rate, 4),
            "mean_return": round(self.mean_return, 6),
            "hit_rate": round(self.hit_rate, 4),
        }


@dataclass(frozen=True)
class MinedRule:
    """从决策树中提取的一条交易规则。"""

    direction: str  # "buy" | "sell"
    conditions: List[RuleCondition]
    regime: Optional[str]  # 在特定 regime 下发现的，None = 全部

    # 训练集表现
    train_hit_rate: float
    train_mean_return: float
    train_n_samples: int

    # 测试集表现
    test_hit_rate: Optional[float]
    test_mean_return: Optional[float]
    test_n_samples: int

    # 树的元信息
    tree_depth: int
    feature_importances: Dict[str, float] = field(default_factory=dict)

    # 统计检验（新增）
    permutation_p_value: Optional[float] = None  # 排列检验 p-value
    binomial_p_value: Optional[float] = None  # 测试集 hit_rate binomial p-value
    cv_consistency: float = 0.0  # 跨 fold 出现一致性
    is_significant: bool = False  # 综合判断

    # F-12d：barrier 统计（train/test 分开，每组 barrier config 一条）
    # 为空 = matrix 未计算 barrier_returns（老数据向后兼容）
    barrier_stats_train: Tuple["BarrierStats", ...] = ()
    barrier_stats_test: Tuple["BarrierStats", ...] = ()

    @property
    def why_conditions(self) -> List[RuleCondition]:
        return [c for c in self.conditions if c.role == "why"]

    @property
    def when_conditions(self) -> List[RuleCondition]:
        return [c for c in self.conditions if c.role == "when"]

    @property
    def where_conditions(self) -> List[RuleCondition]:
        return [c for c in self.conditions if c.role == "where"]

    def rule_string(self) -> str:
        conds = " AND ".join(str(c) for c in self.conditions)
        return f"IF {conds} THEN {self.direction}"

    def structured_string(self) -> str:
        """按 Why/When/Where 结构输出规则。"""
        parts: List[str] = [f"direction: {self.direction}"]
        why = self.why_conditions
        when = self.when_conditions
        where = self.where_conditions
        if why:
            parts.append(f"  Why:   {' AND '.join(str(c) for c in why)}")
        if when:
            parts.append(f"  When:  {' AND '.join(str(c) for c in when)}")
        if where:
            parts.append(f"  Where: {' AND '.join(str(c) for c in where)}")
        return "\n".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "direction": self.direction,
            "rule": self.rule_string(),
            "structured": {
                "why": [c.to_dict() for c in self.why_conditions],
                "when": [c.to_dict() for c in self.when_conditions],
                "where": [c.to_dict() for c in self.where_conditions],
            },
            "conditions": [c.to_dict() for c in self.conditions],
            "regime": self.regime,
            "train": {
                "hit_rate": round(self.train_hit_rate, 4),
                "mean_return": round(self.train_mean_return, 6),
                "n_samples": self.train_n_samples,
            },
            "test": {
                "hit_rate": (
                    round(self.test_hit_rate, 4)
                    if self.test_hit_rate is not None
                    else None
                ),
                "mean_return": (
                    round(self.test_mean_return, 6)
                    if self.test_mean_return is not None
                    else None
                ),
                "n_samples": self.test_n_samples,
            },
            "depth": self.tree_depth,
            "top_features": dict(
                sorted(
                    self.feature_importances.items(),
                    key=lambda x: x[1],
                    reverse=True,
                )[:5]
            ),
            "is_significant": self.is_significant,
        }
        if self.permutation_p_value is not None:
            d["permutation_p_value"] = round(self.permutation_p_value, 4)
        if self.binomial_p_value is not None:
            d["binomial_p_value"] = round(self.binomial_p_value, 4)
        if self.cv_consistency > 0:
            d["cv_consistency"] = round(self.cv_consistency, 2)
        if self.barrier_stats_train:
            d["barrier_stats_train"] = [s.to_dict() for s in self.barrier_stats_train]
        if self.barrier_stats_test:
            d["barrier_stats_test"] = [s.to_dict() for s in self.barrier_stats_test]
        return d


# ── 绝对价格字段过滤 ────────────────────────────────────────────

_DIMENSIONLESS_FIELDS = frozenset(
    {
        # RSI 系列 (0-100)
        "rsi",
        "rsi_d3",
        "rsi_d5",
        # ADX 系列 (0-100)
        "adx",
        "adx_d3",
        "plus_di",
        "minus_di",
        # Stoch RSI (0-100)
        "stoch_rsi_k",
        "stoch_rsi_d",
        # CCI (无界但无量纲)
        "cci",
        # Williams %R (-100~0)
        "williams_r",
        # ROC (百分比)
        "roc",
        # MACD 的 histogram（相对值，随 ATR 缩放但可用）
        "hist",
        # Bar Stats (0-1 比率)
        "body_ratio",
        "upper_shadow_ratio",
        "lower_shadow_ratio",
        "close_position",
        "range_ratio",
        # Bollinger 的 BB width 百分比
        "bb_width_pct",
        # Supertrend 方向 (-1/1)
        "direction",
        # Volume 比率
        "volume_ratio",
        # 复合指标 (composite.py)
        "di_spread",
        "squeeze",
        "squeeze_intensity",
        "vwap_gap_atr",
        "rsi_accel",
        "roc_accel",
        # K 线形态 (candlestick.py)
        "hammer",
        "engulfing",
        "doji",
        "pin_bar",
        "inside_bar",
        "consecutive_dir",
        # 价格结构 (price_structure.py)
        "structure_type",
        "trend_bars",
        "dist_to_swing_high_atr",
        "dist_to_swing_low_atr",
        "swing_range_atr",
        # 跳空 (gap.py)
        "gap_atr",
        "gap_fill_pct",
        "has_gap",
        # 研究特征 (feature_engineer.py)
        "momentum_consensus",
        "regime_entropy",
        "bars_in_regime",
        # Intrabar 派生特征 (feature_engineer.py, group=derived_intrabar)
        "child_bar_consensus",
        "child_range_acceleration",
        "intrabar_momentum_shift",
        "child_volume_front_weight",
        "child_bar_count_ratio",
    }
)


def _is_dimensionless(indicator: str, field: str) -> bool:
    """判断指标字段是否为无量纲（可跨时段泛化）。"""
    return field in _DIMENSIONLESS_FIELDS


@dataclass(frozen=True)
class RuleMiningConfig:
    """规则挖掘配置。"""

    max_depth: int = 3
    min_samples_leaf: int = 30
    min_hit_rate: float = 0.55
    min_test_hit_rate: float = 0.52
    max_rules: int = 20
    dimensionless_only: bool = True
    # 排列检验（新增）
    n_permutations: int = 200
    permutation_significance: float = 0.05
    # CV 一致性（新增）
    cv_folds: int = 5
    cv_consistency_threshold: float = 0.40  # 规则至少在 40% 的 fold 中出现


# ── 核心分析函数 ─────────────────────────────────────────────────


def mine_rules(
    matrix: DataMatrix,
    *,
    horizons: Optional[List[int]] = None,
    regime_filter: Optional[str] = None,
    config: Optional[RuleMiningConfig] = None,
    overfitting_config: Optional[OverfittingConfig] = None,
) -> List[MinedRule]:
    """从 DataMatrix 中挖掘多条件交易规则。

    统计防护：排列检验 + CV 一致性 + binomial 检验。
    """
    cfg = config or RuleMiningConfig()
    of_cfg = overfitting_config or OverfittingConfig()

    if horizons is None:
        horizons = sorted(matrix.forward_returns.keys())

    all_rules: List[MinedRule] = []

    for horizon in horizons:
        fwd = matrix.forward_returns.get(horizon)
        if fwd is None:
            continue

        for direction in ("buy", "sell"):
            rules = _mine_single(
                matrix=matrix,
                forward_returns=fwd,
                forward_bars=horizon,
                direction=direction,
                regime_filter=regime_filter,
                cfg=cfg,
                of_cfg=of_cfg,
            )
            all_rules.extend(rules)

    # 按显著性 + 测试集命中率排序
    all_rules.sort(
        key=lambda r: (
            r.is_significant,  # 显著的排前面
            r.test_hit_rate if r.test_hit_rate is not None else 0.0,
        ),
        reverse=True,
    )

    return all_rules[: cfg.max_rules]


def tag_cross_provider_rules(
    rules: list,
    provider_groups: Optional[Dict[str, List[Tuple[str, str]]]] = None,
) -> list:
    """从规则列表中提取涉及 2+ 个 Provider 的规则。

    跨 Provider 规则意味着规则同时依赖来自不同特征域的条件，
    这类规则更可能具备真实预测力（不太可能因单一数据源的偶然性产生）。

    Args:
        rules:           MinedRule 列表
        provider_groups: {provider_name: [(indicator, field), ...]}

    Returns:
        涉及 2+ 个 Provider 的规则子列表
    """
    if not provider_groups:
        return []

    key_to_provider: Dict[Tuple[str, str], str] = {}
    for prov, keys in provider_groups.items():
        for k in keys:
            key_to_provider[k] = prov

    cross_rules: list = []
    for rule in rules:
        providers_used: set = set()
        for cond in rule.conditions:
            key = (cond.indicator, cond.field)
            prov = key_to_provider.get(key, "base")
            providers_used.add(prov)
        if len(providers_used) >= 2:
            cross_rules.append(rule)
    return cross_rules


def _mine_single(
    *,
    matrix: DataMatrix,
    forward_returns: List[Optional[float]],
    forward_bars: int,
    direction: str,
    regime_filter: Optional[str],
    cfg: RuleMiningConfig,
    of_cfg: OverfittingConfig,
) -> List[MinedRule]:
    """对单个 (horizon, direction) 组合挖掘规则。"""
    if cfg.dimensionless_only:
        feature_names = [
            (ind, fld)
            for ind, fld in matrix.indicator_series.keys()
            if _is_dimensionless(ind, fld)
        ]
    else:
        feature_names = list(matrix.indicator_series.keys())
    if not feature_names:
        return []

    train_X, train_y, train_returns, train_idx = _build_arrays(
        matrix,
        forward_returns,
        feature_names,
        matrix.train_slice(),
        direction,
        regime_filter,
    )
    test_X, test_y, test_returns, test_idx = _build_arrays(
        matrix,
        forward_returns,
        feature_names,
        matrix.test_slice(),
        direction,
        regime_filter,
    )

    if len(train_y) < of_cfg.min_samples:
        return []

    # 训练决策树
    sample_weights = np.abs(train_returns) + 1e-8
    tree = DecisionTreeClassifier(
        max_depth=cfg.max_depth,
        min_samples_leaf=cfg.min_samples_leaf,
        random_state=42,
    )
    tree.fit(train_X, train_y, sample_weight=sample_weights)

    # 提取规则
    raw_rules = _extract_rules(
        tree=tree,
        feature_names=feature_names,
        train_X=train_X,
        train_y=train_y,
        train_returns=train_returns,
        train_idx=train_idx,
        test_X=test_X,
        test_y=test_y,
        test_returns=test_returns,
        test_idx=test_idx,
        direction=direction,
        forward_bars=forward_bars,
        regime_filter=regime_filter,
        cfg=cfg,
        matrix=matrix,
    )

    if not raw_rules:
        return raw_rules

    # ── 统计检验：排列检验 + CV 一致性 + binomial ──────────────

    # 排列检验：打乱 y 后重新训练树，统计 null distribution 下 hit_rate
    perm_null_hit_rates: Optional[List[float]] = None
    if cfg.n_permutations > 0:
        perm_null_hit_rates = _permutation_test_tree(
            train_X=train_X,
            train_y=train_y,
            train_returns=train_returns,
            cfg=cfg,
        )

    # CV 一致性：在各 fold 中分别训练，看规则条件是否稳定
    cv_rule_counts = _cv_rule_consistency(
        matrix=matrix,
        forward_returns=forward_returns,
        feature_names=feature_names,
        direction=direction,
        regime_filter=regime_filter,
        cfg=cfg,
        of_cfg=of_cfg,
    )

    # 为每条规则附加统计检验结果
    enriched_rules: List[MinedRule] = []
    for rule in raw_rules:
        # 排列 p-value：训练集 hit_rate 在 null distribution 中的位置
        perm_p: Optional[float] = None
        if perm_null_hit_rates is not None:
            if len(perm_null_hit_rates) == 0:
                # 排列检验未产生有效结果 → 保守返回非显著
                perm_p = 1.0
            else:
                count_ge = sum(
                    1 for h in perm_null_hit_rates if h >= rule.train_hit_rate
                )
                perm_p = count_ge / len(perm_null_hit_rates)

        # Binomial 检验：测试集 hit_rate 是否显著优于 50%
        binom_p: Optional[float] = None
        if rule.test_hit_rate is not None and rule.test_n_samples > 0:
            hits = int(round(rule.test_hit_rate * rule.test_n_samples))
            binom_p = binomial_test_p(hits, rule.test_n_samples, 0.5)

        # CV 一致性：规则条件在多少个 fold 中出现
        rule_key = _rule_condition_key(rule.conditions)
        cv_score = cv_rule_counts.get(rule_key, 0.0)

        # 综合显著性判断
        is_sig = _judge_significance(
            perm_p=perm_p,
            binom_p=binom_p,
            cv_score=cv_score,
            test_hit_rate=rule.test_hit_rate,
            cfg=cfg,
        )

        enriched_rules.append(
            MinedRule(
                direction=rule.direction,
                conditions=rule.conditions,
                regime=rule.regime,
                train_hit_rate=rule.train_hit_rate,
                train_mean_return=rule.train_mean_return,
                train_n_samples=rule.train_n_samples,
                test_hit_rate=rule.test_hit_rate,
                test_mean_return=rule.test_mean_return,
                test_n_samples=rule.test_n_samples,
                tree_depth=rule.tree_depth,
                feature_importances=rule.feature_importances,
                permutation_p_value=perm_p,
                binomial_p_value=binom_p,
                cv_consistency=cv_score,
                is_significant=is_sig,
                # F-12d：不要在统计检验环节丢 barrier_stats（raw rule 已经计算完）
                barrier_stats_train=rule.barrier_stats_train,
                barrier_stats_test=rule.barrier_stats_test,
            )
        )

    return enriched_rules


def _judge_significance(
    *,
    perm_p: Optional[float],
    binom_p: Optional[float],
    cv_score: float,
    test_hit_rate: Optional[float],
    cfg: RuleMiningConfig,
) -> bool:
    """综合三项检验判断规则是否显著。

    至少满足以下两项中的一项（宽松 OR）：
      A) 排列检验 p ≤ 0.05 + CV 一致性 ≥ threshold
      B) binomial p ≤ 0.05 + test hit_rate ≥ min_test_hit_rate
    """
    path_a = (
        perm_p is not None
        and perm_p <= cfg.permutation_significance
        and cv_score >= cfg.cv_consistency_threshold
    )

    path_b = (
        binom_p is not None
        and binom_p <= 0.05
        and test_hit_rate is not None
        and test_hit_rate >= cfg.min_test_hit_rate
    )

    return path_a or path_b


# ── 排列检验 ──────────────────────────────────────────────────


def _permutation_test_tree(
    *,
    train_X: np.ndarray,
    train_y: np.ndarray,
    train_returns: np.ndarray,
    cfg: RuleMiningConfig,
    seed: int = 42,
) -> List[float]:
    """排列检验：打乱标签后训练树，收集 null distribution 下最优叶节点 hit_rate。

    返回 null distribution 的 hit_rate 列表（每次排列一个最大 hit_rate）。
    """
    n = len(train_y)
    if n < 10:
        return []

    rng = random.Random(seed)
    adaptive_bs = auto_block_size(train_y.tolist())
    null_hit_rates: List[float] = []

    for _ in range(cfg.n_permutations):
        # Block shuffle 标签（保留局部自相关）
        shuffled_y = np.array(
            block_shuffle(train_y.tolist(), adaptive_bs, rng), dtype=train_y.dtype
        )

        sample_weights = np.abs(train_returns) + 1e-8
        perm_tree = DecisionTreeClassifier(
            max_depth=cfg.max_depth,
            min_samples_leaf=cfg.min_samples_leaf,
            random_state=None,  # 不固定种子，增加排列多样性
        )
        try:
            perm_tree.fit(train_X, shuffled_y, sample_weight=sample_weights)
        except Exception:
            continue

        # 提取 null tree 中最优叶节点 hit_rate
        best_hr = _best_leaf_hit_rate(perm_tree, train_X, shuffled_y)
        null_hit_rates.append(best_hr)

    return null_hit_rates


def _best_leaf_hit_rate(
    tree: DecisionTreeClassifier,
    X: np.ndarray,
    y: np.ndarray,
) -> float:
    """从树中找到预测 class=1 的最高 hit_rate 叶节点。"""
    tree_ = tree.tree_
    predictions = tree.apply(X)  # 每个样本的叶节点 ID
    leaf_ids = set(predictions)

    best_hr = 0.5
    for leaf_id in leaf_ids:
        mask = predictions == leaf_id
        n_leaf = int(np.sum(mask))
        if n_leaf < 5:
            continue
        hits = int(np.sum(y[mask] == 1))
        hr = hits / n_leaf
        if hr > best_hr:
            best_hr = hr

    return best_hr


# ── CV 一致性 ──────────────────────────────────────────────────


def _rule_condition_key(conditions: List[RuleCondition]) -> str:
    """生成规则条件的规范化 key（用于跨 fold 匹配）。

    只看指标+方向，不看精确阈值（同一模式在不同 fold 中阈值会略有不同）。
    """
    parts = sorted(f"{c.indicator}.{c.field}{c.operator}" for c in conditions)
    return "|".join(parts)


def _cv_rule_consistency(
    *,
    matrix: DataMatrix,
    forward_returns: List[Optional[float]],
    feature_names: List[Tuple[str, str]],
    direction: str,
    regime_filter: Optional[str],
    cfg: RuleMiningConfig,
    of_cfg: OverfittingConfig,
) -> Dict[str, float]:
    """在时序 CV 各 fold 中训练树，统计规则条件的出现频率。

    Returns:
        {rule_condition_key: 出现比例 (0.0 ~ 1.0)}
    """
    from ..core.overfitting import time_series_cv_splits

    train_range = matrix.train_slice()
    folds = time_series_cv_splits(
        n_samples=len(train_range),
        n_folds=cfg.cv_folds,
        min_train_size=max(of_cfg.min_samples, 50),
        mode=of_cfg.cv_mode,
    )

    if not folds:
        return {}

    rule_appearances: Dict[str, int] = {}
    valid_folds = 0
    base_start = train_range.start

    for fold in folds:
        fold_train_range = range(
            base_start + fold.train_start, base_start + fold.train_end
        )
        fold_X, fold_y, fold_returns, fold_idx = _build_arrays(
            matrix,
            forward_returns,
            feature_names,
            fold_train_range,
            direction,
            regime_filter,
        )
        if len(fold_y) < of_cfg.min_samples:
            continue

        valid_folds += 1
        sample_weights = np.abs(fold_returns) + 1e-8
        fold_tree = DecisionTreeClassifier(
            max_depth=cfg.max_depth,
            min_samples_leaf=cfg.min_samples_leaf,
            random_state=42,
        )
        try:
            fold_tree.fit(fold_X, fold_y, sample_weight=sample_weights)
        except Exception:
            continue

        # 提取此 fold 中的规则条件 key
        # CV 一致性只关心 rule condition key，不需要 barrier 统计 → matrix=None
        fold_rules = _extract_rules(
            tree=fold_tree,
            feature_names=feature_names,
            train_X=fold_X,
            train_y=fold_y,
            train_returns=fold_returns,
            train_idx=fold_idx,
            test_X=np.empty((0, len(feature_names))),
            test_y=np.empty(0),
            test_returns=np.empty(0),
            test_idx=np.empty(0, dtype=np.int64),
            direction=direction,
            forward_bars=0,
            regime_filter=regime_filter,
            cfg=cfg,
            matrix=None,
        )
        seen_keys: set[str] = set()
        for r in fold_rules:
            key = _rule_condition_key(r.conditions)
            if key not in seen_keys:
                seen_keys.add(key)
                rule_appearances[key] = rule_appearances.get(key, 0) + 1

    if valid_folds == 0:
        return {}

    return {key: count / valid_folds for key, count in rule_appearances.items()}


# ── 数组构建 ──────────────────────────────────────────────────


def _build_arrays(
    matrix: DataMatrix,
    forward_returns: List[Optional[float]],
    feature_names: List[Tuple[str, str]],
    idx_range: range,
    direction: str,
    regime_filter: Optional[str],
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """构建 sklearn 需要的特征矩阵 X / 标签 y / return / bar_indices。

    bar_indices 保留过滤后样本在原始 matrix 里的 bar 索引，用于后续
    barrier_returns 查表（F-12d）。
    """
    rows_X: List[List[float]] = []
    rows_y: List[int] = []
    rows_ret: List[float] = []
    rows_idx: List[int] = []

    for i in idx_range:
        fwd = forward_returns[i]
        if fwd is None:
            continue
        if regime_filter is not None and matrix.regimes[i].value != regime_filter:
            continue

        row: List[float] = []
        skip = False
        for ind_name, field_name in feature_names:
            val = matrix.indicator_series[(ind_name, field_name)][i]
            if val is None:
                skip = True
                break
            row.append(val)
        if skip:
            continue

        if direction == "buy":
            label = 1 if fwd > 0 else 0
            ret = fwd
        else:
            label = 1 if fwd < 0 else 0
            ret = -fwd

        rows_X.append(row)
        rows_y.append(label)
        rows_ret.append(ret)
        rows_idx.append(i)

    if not rows_X:
        return (
            np.empty((0, len(feature_names))),
            np.empty(0),
            np.empty(0),
            np.empty(0, dtype=np.int64),
        )

    return (
        np.array(rows_X, dtype=np.float64),
        np.array(rows_y, dtype=np.int32),
        np.array(rows_ret, dtype=np.float64),
        np.array(rows_idx, dtype=np.int64),
    )


def _compute_barrier_stats_for_rule(
    matrix: DataMatrix,
    direction: str,
    bar_indices: np.ndarray,
) -> Tuple[BarrierStats, ...]:
    """F-12d：给定规则触发的 bar 索引集合，对每组 barrier config 汇总退出分布。

    若 matrix 未填充 barrier_returns（老数据）返回空元组。
    若 bar_indices 空返回空元组。
    """
    source = (
        matrix.barrier_returns_long
        if direction == "buy"
        else matrix.barrier_returns_short
    )
    if not source or bar_indices.size == 0:
        return ()

    results: List[BarrierStats] = []
    for key, outcomes in source.items():
        tp_count = 0
        sl_count = 0
        time_count = 0
        returns: List[float] = []
        hits = 0
        for idx in bar_indices:
            if idx < 0 or idx >= len(outcomes):
                continue
            outcome = outcomes[idx]
            if outcome is None:
                continue
            if outcome.barrier == "tp":
                tp_count += 1
            elif outcome.barrier == "sl":
                sl_count += 1
            else:
                time_count += 1
            returns.append(outcome.return_pct)
            if outcome.return_pct > 0:
                hits += 1

        n = len(returns)
        if n == 0:
            continue
        results.append(
            BarrierStats(
                barrier_key=key,
                n_samples=n,
                tp_rate=tp_count / n,
                sl_rate=sl_count / n,
                time_rate=time_count / n,
                mean_return=float(np.mean(returns)),
                hit_rate=hits / n,
            )
        )
    # 按 hit_rate 降序，便于下游 "找最优 barrier 组合"
    results.sort(key=lambda s: s.hit_rate, reverse=True)
    return tuple(results)


# ── 规则提取 ──────────────────────────────────────────────────


def _extract_rules(
    *,
    tree: DecisionTreeClassifier,
    feature_names: List[Tuple[str, str]],
    train_X: np.ndarray,
    train_y: np.ndarray,
    train_returns: np.ndarray,
    train_idx: np.ndarray,
    test_X: np.ndarray,
    test_y: np.ndarray,
    test_returns: np.ndarray,
    test_idx: np.ndarray,
    direction: str,
    forward_bars: int,
    regime_filter: Optional[str],
    cfg: RuleMiningConfig,
    matrix: Optional[DataMatrix] = None,
) -> List[MinedRule]:
    """从训练好的决策树中提取可解释规则。

    matrix 传入用于在叶节点查 barrier_returns 算 barrier_stats（F-12d）。
    向后兼容：传 None 等同于跳过 barrier 统计。
    """
    tree_ = tree.tree_
    feature_idx = tree_.feature
    threshold = tree_.threshold
    children_left = tree_.children_left
    children_right = tree_.children_right

    importances = tree.feature_importances_
    feat_imp: Dict[str, float] = {}
    for idx, imp in enumerate(importances):
        if imp > 0.01:
            ind, fld = feature_names[idx]
            feat_imp[f"{ind}.{fld}"] = round(float(imp), 3)

    rules: List[MinedRule] = []

    def _traverse(node_id: int, conditions: List[RuleCondition], depth: int) -> None:
        if children_left[node_id] == children_right[node_id]:
            _process_leaf(
                node_id,
                conditions,
                depth,
                rules,
                tree_,
                train_X,
                train_y,
                train_returns,
                train_idx,
                test_X,
                test_y,
                test_returns,
                test_idx,
                feature_names,
                direction,
                forward_bars,
                regime_filter,
                cfg,
                feat_imp,
                matrix,
            )
            return

        feat_idx = feature_idx[node_id]
        thresh = threshold[node_id]
        ind_name, field_name = feature_names[feat_idx]
        role = _classify_role(ind_name)

        left_cond = RuleCondition(
            indicator=ind_name,
            field=field_name,
            operator="<=",
            threshold=float(thresh),
            role=role,
        )
        _traverse(children_left[node_id], conditions + [left_cond], depth + 1)

        right_cond = RuleCondition(
            indicator=ind_name,
            field=field_name,
            operator=">",
            threshold=float(thresh),
            role=role,
        )
        _traverse(children_right[node_id], conditions + [right_cond], depth + 1)

    _traverse(0, [], 0)
    return rules


def _process_leaf(
    node_id: int,
    conditions: List[RuleCondition],
    depth: int,
    rules: List[MinedRule],
    tree_: Any,
    train_X: np.ndarray,
    train_y: np.ndarray,
    train_returns: np.ndarray,
    train_idx: np.ndarray,
    test_X: np.ndarray,
    test_y: np.ndarray,
    test_returns: np.ndarray,
    test_idx: np.ndarray,
    feature_names: List[Tuple[str, str]],
    direction: str,
    forward_bars: int,
    regime_filter: Optional[str],
    cfg: RuleMiningConfig,
    feat_imp: Dict[str, float],
    matrix: Optional[DataMatrix] = None,
) -> None:
    """处理叶节点：检查是否为有价值的规则。"""
    values = tree_.value[node_id][0]
    predicted_class = int(np.argmax(values))

    if predicted_class != 1:
        return

    if not conditions:
        return

    train_mask = _apply_conditions(train_X, conditions, feature_names)
    train_matched = int(np.sum(train_mask))
    if train_matched < cfg.min_samples_leaf:
        return

    train_hits = int(np.sum(train_y[train_mask] == 1))
    train_hit_rate = train_hits / train_matched
    train_mean_ret = float(np.mean(train_returns[train_mask]))

    if train_hit_rate < cfg.min_hit_rate:
        return

    test_hit_rate: Optional[float] = None
    test_mean_ret: Optional[float] = None
    test_matched = 0
    test_mask: Optional[np.ndarray] = None

    if len(test_y) > 0:
        test_mask = _apply_conditions(test_X, conditions, feature_names)
        test_matched = int(np.sum(test_mask))
        if test_matched > 0:
            test_hits = int(np.sum(test_y[test_mask] == 1))
            test_hit_rate = test_hits / test_matched
            test_mean_ret = float(np.mean(test_returns[test_mask]))

    if test_hit_rate is not None and test_hit_rate < cfg.min_test_hit_rate:
        return

    # F-12d：在该规则触发的 bar 集合上计算各 barrier config 的退出分布
    barrier_stats_train: Tuple[BarrierStats, ...] = ()
    barrier_stats_test: Tuple[BarrierStats, ...] = ()
    if matrix is not None:
        train_rule_indices = train_idx[train_mask]
        barrier_stats_train = _compute_barrier_stats_for_rule(
            matrix, direction, train_rule_indices
        )
        if test_mask is not None and test_matched > 0:
            test_rule_indices = test_idx[test_mask]
            barrier_stats_test = _compute_barrier_stats_for_rule(
                matrix, direction, test_rule_indices
            )

    rules.append(
        MinedRule(
            direction=direction,
            conditions=conditions,
            regime=regime_filter,
            train_hit_rate=train_hit_rate,
            train_mean_return=train_mean_ret,
            train_n_samples=train_matched,
            test_hit_rate=test_hit_rate,
            test_mean_return=test_mean_ret,
            test_n_samples=test_matched,
            tree_depth=depth,
            feature_importances=feat_imp,
            barrier_stats_train=barrier_stats_train,
            barrier_stats_test=barrier_stats_test,
        )
    )


def _apply_conditions(
    X: np.ndarray,
    conditions: List[RuleCondition],
    feature_names: List[Tuple[str, str]],
) -> np.ndarray:
    """在特征矩阵上应用规则条件，返回布尔 mask。"""
    mask = np.ones(len(X), dtype=bool)
    name_to_idx = {name: i for i, name in enumerate(feature_names)}

    for cond in conditions:
        key = (cond.indicator, cond.field)
        idx = name_to_idx.get(key)
        if idx is None:
            mask[:] = False
            break
        col = X[:, idx]
        if cond.operator == "<=":
            mask &= col <= cond.threshold
        else:
            mask &= col > cond.threshold

    return mask
