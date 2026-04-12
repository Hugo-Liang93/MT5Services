"""Feature Engineering 框架 — 从原始指标派生跨指标/时序/结构特征。

可插拔架构：每个派生特征是一个 FeatureDefinition，注册到 FeatureEngineer。
DataMatrix 是 frozen，enrich() 通过 dataclasses.replace() 创建新实例。

所有内置特征均为无量纲，兼容 rule_mining 的 _DIMENSIONLESS_FIELDS。
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core.data_matrix import DataMatrix

# 特征计算函数类型：(matrix, bar_index) -> Optional[float]
FeatureFunc = Callable[[DataMatrix, int], Optional[float]]


@dataclass(frozen=True)
class FeatureDefinition:
    """派生特征定义。"""

    name: str  # e.g. "di_spread"
    group: str  # indicator_series 中的 key group, e.g. "derived"
    func: FeatureFunc
    dependencies: Tuple[Tuple[str, str], ...]  # 需要的 indicator_series key
    is_dimensionless: bool = True
    formula_summary: str = ""
    source_inputs: Tuple[str, ...] = ()
    runtime_state_inputs: Tuple[str, ...] = ()
    live_computable: bool = True
    compute_scope: str = "bar_close"
    bounded_lookback: bool = True
    strategy_roles: Tuple[str, ...] = ()
    promotion_target_default: str = "research_only"
    no_lookahead: bool = True
    interpretable: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "group": self.group,
            "dependencies": [f"{ind}.{field}" for ind, field in self.dependencies],
            "is_dimensionless": self.is_dimensionless,
            "formula_summary": self.formula_summary,
            "source_inputs": list(self.source_inputs),
            "runtime_state_inputs": list(self.runtime_state_inputs),
            "live_computable": self.live_computable,
            "compute_scope": self.compute_scope,
            "bounded_lookback": self.bounded_lookback,
            "strategy_roles": list(self.strategy_roles),
            "promotion_target_default": self.promotion_target_default,
            "no_lookahead": self.no_lookahead,
            "interpretable": self.interpretable,
        }


class FeatureEngineer:
    """派生特征注册和批量计算。"""

    def __init__(self) -> None:
        self._features: Dict[str, FeatureDefinition] = {}

    def register(self, defn: FeatureDefinition) -> None:
        self._features[defn.name] = defn

    def available_features(self) -> List[str]:
        return list(self._features.keys())

    def definition(self, name: str) -> Optional[FeatureDefinition]:
        return self._features.get(name)

    def definitions(self) -> Dict[str, FeatureDefinition]:
        return dict(self._features)

    def inventory(self) -> Dict[str, Any]:
        return {
            "active_features": {
                name: defn.to_dict() for name, defn in sorted(self._features.items())
            },
            "promoted_indicator_precedents": list(PROMOTED_INDICATOR_PRECEDENTS),
        }

    def enrich(
        self,
        matrix: DataMatrix,
        *,
        feature_names: Optional[List[str]] = None,
    ) -> DataMatrix:
        """计算派生特征并返回增强的 DataMatrix。

        Args:
            matrix: 原始 DataMatrix（不修改）
            feature_names: 要计算的特征列表（None = 全部已注册）

        Returns:
            包含额外 indicator_series 条目的新 DataMatrix
        """
        targets = feature_names or list(self._features.keys())
        new_series = dict(matrix.indicator_series)

        for fname in targets:
            defn = self._features.get(fname)
            if defn is None:
                continue
            # 检查依赖是否存在
            if not all(dep in matrix.indicator_series for dep in defn.dependencies):
                continue
            # 逐 bar 计算
            series: List[Optional[float]] = []
            for i in range(matrix.n_bars):
                try:
                    val = defn.func(matrix, i)
                except Exception:
                    val = None
                series.append(val)
            new_series[(defn.group, defn.name)] = series

        return dataclasses.replace(matrix, indicator_series=new_series)


# ── 内置派生特征 ──────────────────────────────────────────────


def _get(matrix: DataMatrix, ind: str, field: str, i: int) -> Optional[float]:
    """安全获取 indicator_series 值。"""
    series = matrix.indicator_series.get((ind, field))
    if series is None or i >= len(series):
        return None
    return series[i]


def _di_spread(matrix: DataMatrix, i: int) -> Optional[float]:
    """方向指数差异：(+DI - -DI) / (+DI + -DI + ε)。值域 [-1, 1]。"""
    plus = _get(matrix, "adx14", "plus_di", i)
    minus = _get(matrix, "adx14", "minus_di", i)
    if plus is None or minus is None:
        return None
    return (plus - minus) / (plus + minus + 1e-6)


def _momentum_consensus(matrix: DataMatrix, i: int) -> Optional[float]:
    """动量一致性：(sign(MACD hist) + sign(RSI-50) + sign(StochK-50)) / 3。值域 [-1, 1]。"""
    hist = _get(matrix, "macd", "hist", i)
    rsi = _get(matrix, "rsi14", "rsi", i)
    stoch = _get(matrix, "stoch_rsi14", "stoch_rsi_k", i)
    if hist is None or rsi is None or stoch is None:
        return None
    s = (
        (1.0 if hist > 0 else -1.0 if hist < 0 else 0.0)
        + (1.0 if rsi > 50 else -1.0 if rsi < 50 else 0.0)
        + (1.0 if stoch > 50 else -1.0 if stoch < 50 else 0.0)
    ) / 3.0
    return s


def _squeeze_score(matrix: DataMatrix, i: int) -> Optional[float]:
    """波动率挤压：BB width < KC width → 1.0，否则 0.0。"""
    bb_upper = _get(matrix, "boll20", "bb_upper", i)
    bb_lower = _get(matrix, "boll20", "bb_lower", i)
    kc_upper = _get(matrix, "keltner20", "kc_upper", i)
    kc_lower = _get(matrix, "keltner20", "kc_lower", i)
    if any(v is None for v in (bb_upper, bb_lower, kc_upper, kc_lower)):
        return None
    bb_width = bb_upper - bb_lower  # type: ignore[operator]
    kc_width = kc_upper - kc_lower  # type: ignore[operator]
    return 1.0 if bb_width < kc_width else 0.0


def _vwap_gap_atr(matrix: DataMatrix, i: int) -> Optional[float]:
    """VWAP 偏离度（ATR 归一化）：(close - VWAP) / ATR。无量纲。"""
    close = matrix.closes[i]
    vwap = _get(matrix, "vwap30", "vwap", i)
    atr = _get(matrix, "atr14", "atr", i)
    if vwap is None or atr is None or atr < 1e-12:
        return None
    return (close - vwap) / atr


def _rsi_accel(matrix: DataMatrix, i: int) -> Optional[float]:
    """RSI 加速度（二阶导）：rsi_d3[i] - rsi_d3[i-3]。"""
    series = matrix.indicator_series.get(("rsi14", "rsi_d3"))
    if series is None or i < 3:
        return None
    cur = series[i]
    prev = series[i - 3]
    if cur is None or prev is None:
        return None
    return cur - prev


def _regime_entropy(matrix: DataMatrix, i: int) -> Optional[float]:
    """Regime 概率熵：-Σ(p·ln(p))。值越高 = regime 越不确定。"""
    soft = matrix.soft_regimes[i]
    if soft is None:
        return None
    probs = [p for p in soft.values() if p > 0]
    if not probs:
        return None
    return -sum(p * math.log(p) for p in probs)


def _bars_in_regime(matrix: DataMatrix, i: int) -> Optional[float]:
    """当前 regime 持续 bar 数（O(1) per call，利用上一个 bar 的结果）。

    注意：FeatureEngineer 按 i=0,1,2... 顺序调用，所以可以用全局缓存。
    """
    # 使用函数属性缓存上次结果
    cache = getattr(_bars_in_regime, "_cache", None)
    if cache is None or cache.get("_matrix_id") != id(matrix):
        # 首次调用或矩阵变了：O(n) 预计算全部
        n = matrix.n_bars
        counts = [1.0] * n
        for j in range(1, n):
            if matrix.regimes[j] == matrix.regimes[j - 1]:
                counts[j] = counts[j - 1] + 1.0
        _bars_in_regime._cache = {"_matrix_id": id(matrix), "counts": counts}  # type: ignore[attr-defined]
        cache = _bars_in_regime._cache  # type: ignore[attr-defined]
    return cache["counts"][i] if i < len(cache["counts"]) else None


# ── 特征注册表 ────────────────────────────────────────────────


# 注意：di_spread, squeeze_score, vwap_gap_atr, rsi_accel 已提升为正式指标
# （src/indicators/core/composite.py），不再作为研究特征。
# 其余条目保留在 research feature registry 中，用于继续做候选发现与晋升审计。

_BUILTIN_FEATURES: List[FeatureDefinition] = [
    FeatureDefinition(
        name="momentum_consensus",
        group="derived",
        func=_momentum_consensus,
        dependencies=(
            ("macd", "hist"),
            ("rsi14", "rsi"),
            ("stoch_rsi14", "stoch_rsi_k"),
        ),
        formula_summary=(
            "(sign(macd.hist) + sign(rsi14.rsi-50) + "
            "sign(stoch_rsi14.stoch_rsi_k-50)) / 3"
        ),
        source_inputs=("macd.hist", "rsi14.rsi", "stoch_rsi14.stoch_rsi_k"),
        runtime_state_inputs=(),
        live_computable=True,
        compute_scope="bar_close",
        bounded_lookback=True,
        strategy_roles=("why", "when"),
        promotion_target_default="indicator_and_strategy_candidate",
    ),
    FeatureDefinition(
        name="regime_entropy",
        group="derived",
        func=_regime_entropy,
        dependencies=(),  # 使用 soft_regimes，不是 indicator_series
        formula_summary="-Σ(p·ln(p)) from soft regime probabilities",
        source_inputs=(),
        runtime_state_inputs=("soft_regimes",),
        live_computable=False,
        compute_scope="runtime_state",
        bounded_lookback=True,
        strategy_roles=("why",),
        promotion_target_default="research_only",
    ),
    FeatureDefinition(
        name="bars_in_regime",
        group="derived",
        func=_bars_in_regime,
        dependencies=(),  # 使用 regimes 列表
        formula_summary="count consecutive bars staying in the current hard regime",
        source_inputs=(),
        runtime_state_inputs=("regimes",),
        live_computable=False,
        compute_scope="runtime_state",
        bounded_lookback=True,
        strategy_roles=("when", "where"),
        promotion_target_default="strategy_helper",
    ),
]


PROMOTED_INDICATOR_PRECEDENTS: Tuple[Dict[str, Any], ...] = (
    {
        "feature_name": "di_spread",
        "promoted_indicator_name": "di_spread14",
        "status": "promoted_indicator",
        "note": "已晋升为共享趋势方向复合指标",
    },
    {
        "feature_name": "squeeze_score",
        "promoted_indicator_name": "squeeze20",
        "status": "promoted_indicator",
        "note": "已晋升为共享波动率挤压指标",
    },
    {
        "feature_name": "vwap_gap_atr",
        "promoted_indicator_name": "vwap_dev30",
        "status": "promoted_indicator",
        "note": "已晋升为共享均值偏离指标",
    },
    {
        "feature_name": "rsi_accel",
        "promoted_indicator_name": "momentum_accel14",
        "status": "promoted_indicator",
        "note": "已晋升为共享动量加速度指标",
    },
    {
        "feature_name": "momentum_consensus",
        "promoted_indicator_name": "momentum_consensus14",
        "status": "promoted_indicator",
        "note": "首轮 feature promotion 交付的新共享动量一致性指标",
    },
)


def build_default_engineer() -> FeatureEngineer:
    """创建包含所有内置派生特征的 FeatureEngineer。"""
    eng = FeatureEngineer()
    for defn in _BUILTIN_FEATURES:
        eng.register(defn)
    return eng


def get_feature_definition(name: str) -> Optional[FeatureDefinition]:
    return build_default_engineer().definition(name)


def get_feature_inventory() -> Dict[str, Any]:
    return build_default_engineer().inventory()
