"""规则挖掘分析器 — 用决策树从数据中提取多条件交易规则。

纯数据驱动：(indicator_values) → (future_return > 0?) 的分类问题。
用 sklearn DecisionTree 拟合后，提取可解释的 IF-THEN 规则。

输出映射到 StructuredStrategyBase 的 Why/When/Where 三层框架：
  - Why（硬门控）：趋势/方向类指标 → 确定方向
  - When（硬门控）：时机/振荡类指标 → 入场时机
  - Where（软门控）：价格结构类指标 → 结构位加分

设计原则：
  - 树深度限制（max_depth=3~4）确保规则可解释
  - 每个叶节点最小样本数确保统计可靠
  - Train/test 分割防过拟合
  - 仅输出 hit_rate > 50% 的"盈利方向"叶节点
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from sklearn.tree import DecisionTreeClassifier

from src.signals.evaluation.regime import RegimeType

from ..config import OverfittingConfig
from ..data_matrix import DataMatrix

logger = logging.getLogger(__name__)


# ── 结果模型 ────────────────────────────────────────────────────


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
        return {
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
        }


# ── 绝对价格字段过滤 ────────────────────────────────────────────
# 这些字段的值是绝对价格（随行情变化），决策树会在历史价位上切分，
# 产出的规则无法泛化到未来。只保留无量纲/归一化的指标字段。

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
    }
)


def _is_dimensionless(indicator: str, field: str) -> bool:
    """判断指标字段是否为无量纲（可跨时段泛化）。"""
    return field in _DIMENSIONLESS_FIELDS


@dataclass(frozen=True)
class RuleMiningConfig:
    """规则挖掘配置。"""

    max_depth: int = 3  # 树最大深度（3=最多3个条件组合）
    min_samples_leaf: int = 30  # 叶节点最小样本数
    min_hit_rate: float = 0.55  # 最低命中率才输出规则
    min_test_hit_rate: float = 0.52  # 测试集最低命中率
    max_rules: int = 20  # 最多输出规则数
    dimensionless_only: bool = True  # 仅使用无量纲特征


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

    Args:
        matrix: DataMatrix 实例
        horizons: 前瞻周期列表（默认用 matrix 中所有可用的）
        regime_filter: 仅在特定 regime 下挖掘（None = 全部数据）
        config: 规则挖掘配置
        overfitting_config: 过拟合防护配置

    Returns:
        按测试集命中率降序排列的规则列表
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

    # 按测试集命中率排序，无测试数据的排最后
    all_rules.sort(
        key=lambda r: (r.test_hit_rate if r.test_hit_rate is not None else 0.0),
        reverse=True,
    )

    return all_rules[: cfg.max_rules]


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
    # 构建特征矩阵和标签（过滤绝对价格字段）
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

    train_X, train_y, train_returns = _build_arrays(
        matrix,
        forward_returns,
        feature_names,
        matrix.train_slice(),
        direction,
        regime_filter,
    )
    test_X, test_y, test_returns = _build_arrays(
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
    # 使用 |forward_return| 作为 sample_weight，让大幅盈亏的样本有更高权重
    # 这比 class_weight="balanced" 更合理：亏 1R 比错过 1R 严重
    sample_weights = np.abs(train_returns) + 1e-8  # 避免零权重
    tree = DecisionTreeClassifier(
        max_depth=cfg.max_depth,
        min_samples_leaf=cfg.min_samples_leaf,
        random_state=42,
    )
    tree.fit(train_X, train_y, sample_weight=sample_weights)

    # 提取规则
    rules = _extract_rules(
        tree=tree,
        feature_names=feature_names,
        train_X=train_X,
        train_y=train_y,
        train_returns=train_returns,
        test_X=test_X,
        test_y=test_y,
        test_returns=test_returns,
        direction=direction,
        forward_bars=forward_bars,
        regime_filter=regime_filter,
        cfg=cfg,
    )

    return rules


def _build_arrays(
    matrix: DataMatrix,
    forward_returns: List[Optional[float]],
    feature_names: List[Tuple[str, str]],
    idx_range: range,
    direction: str,
    regime_filter: Optional[str],
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """构建 sklearn 需要的特征矩阵 X 和标签 y。"""
    rows_X: List[List[float]] = []
    rows_y: List[int] = []
    rows_ret: List[float] = []

    for i in idx_range:
        fwd = forward_returns[i]
        if fwd is None:
            continue
        if regime_filter is not None and matrix.regimes[i].value != regime_filter:
            continue

        # 构建特征行
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

        # 标签：该方向是否盈利
        if direction == "buy":
            label = 1 if fwd > 0 else 0
            ret = fwd
        else:  # sell
            label = 1 if fwd < 0 else 0
            ret = -fwd

        rows_X.append(row)
        rows_y.append(label)
        rows_ret.append(ret)

    if not rows_X:
        return np.empty((0, len(feature_names))), np.empty(0), np.empty(0)

    return (
        np.array(rows_X, dtype=np.float64),
        np.array(rows_y, dtype=np.int32),
        np.array(rows_ret, dtype=np.float64),
    )


def _extract_rules(
    *,
    tree: DecisionTreeClassifier,
    feature_names: List[Tuple[str, str]],
    train_X: np.ndarray,
    train_y: np.ndarray,
    train_returns: np.ndarray,
    test_X: np.ndarray,
    test_y: np.ndarray,
    test_returns: np.ndarray,
    direction: str,
    forward_bars: int,
    regime_filter: Optional[str],
    cfg: RuleMiningConfig,
) -> List[MinedRule]:
    """从训练好的决策树中提取可解释规则。"""
    tree_ = tree.tree_
    feature_idx = tree_.feature
    threshold = tree_.threshold
    children_left = tree_.children_left
    children_right = tree_.children_right

    # 特征重要度
    importances = tree.feature_importances_
    feat_imp: Dict[str, float] = {}
    for idx, imp in enumerate(importances):
        if imp > 0.01:
            ind, fld = feature_names[idx]
            feat_imp[f"{ind}.{fld}"] = round(float(imp), 3)

    rules: List[MinedRule] = []

    # DFS 遍历所有叶节点
    def _traverse(node_id: int, conditions: List[RuleCondition], depth: int) -> None:
        # 叶节点
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
                test_X,
                test_y,
                test_returns,
                feature_names,
                direction,
                forward_bars,
                regime_filter,
                cfg,
                feat_imp,
            )
            return

        # 内部节点：左子树 (feature <= threshold)
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

        # 右子树 (feature > threshold)
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
    test_X: np.ndarray,
    test_y: np.ndarray,
    test_returns: np.ndarray,
    feature_names: List[Tuple[str, str]],
    direction: str,
    forward_bars: int,
    regime_filter: Optional[str],
    cfg: RuleMiningConfig,
    feat_imp: Dict[str, float],
) -> None:
    """处理叶节点：检查是否为有价值的规则。"""
    # 叶节点预测类别：value[node_id] = [[n_class0, n_class1]]
    values = tree_.value[node_id][0]
    predicted_class = int(np.argmax(values))

    # 只关注预测为"盈利"的叶节点
    if predicted_class != 1:
        return

    if not conditions:
        return

    # 在训练集上应用条件，计算表现
    train_mask = _apply_conditions(train_X, conditions, feature_names)
    train_matched = int(np.sum(train_mask))
    if train_matched < cfg.min_samples_leaf:
        return

    train_hits = int(np.sum(train_y[train_mask] == 1))
    train_hit_rate = train_hits / train_matched
    train_mean_ret = float(np.mean(train_returns[train_mask]))

    if train_hit_rate < cfg.min_hit_rate:
        return

    # 在测试集上验证
    test_hit_rate: Optional[float] = None
    test_mean_ret: Optional[float] = None
    test_matched = 0

    if len(test_y) > 0:
        test_mask = _apply_conditions(test_X, conditions, feature_names)
        test_matched = int(np.sum(test_mask))
        if test_matched > 0:
            test_hits = int(np.sum(test_y[test_mask] == 1))
            test_hit_rate = test_hits / test_matched
            test_mean_ret = float(np.mean(test_returns[test_mask]))

    # 测试集命中率门槛
    if test_hit_rate is not None and test_hit_rate < cfg.min_test_hit_rate:
        return

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
        else:  # ">"
            mask &= col > cond.threshold

    return mask
