"""信号挖掘结果模型。

遵循项目惯例：内部结果用 frozen dataclass + to_dict()，
与 BacktestMetrics / MonteCarloResult / CorrelationAnalysis 一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ── 通用 ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DataSummary:
    """DataMatrix 的数据摘要。"""

    symbol: str
    timeframe: str
    n_bars: int
    start_time: datetime
    end_time: datetime
    train_bars: int
    test_bars: int
    regime_distribution: Dict[str, int]
    available_indicators: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "n_bars": self.n_bars,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "train_bars": self.train_bars,
            "test_bars": self.test_bars,
            "regime_distribution": self.regime_distribution,
            "available_indicators": self.available_indicators,
        }


# ── Rolling IC ─────────────────────────────────────────────────


@dataclass(frozen=True)
class RollingICResult:
    """滑动窗口 IC 统计。"""

    mean_ic: float
    std_ic: float
    information_ratio: float  # mean_ic / std_ic, IR > 0.5 = 稳定
    n_windows: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mean_ic": round(self.mean_ic, 4),
            "std_ic": round(self.std_ic, 4),
            "information_ratio": round(self.information_ratio, 4),
            "n_windows": self.n_windows,
        }


# ── Predictive Power ────────────────────────────────────────────


@dataclass(frozen=True)
class IndicatorPredictiveResult:
    """单个 (indicator_field, horizon, regime) 组合的预测力结果。"""

    indicator_name: str
    field_name: str
    forward_bars: int
    regime: Optional[str]  # None = 全部 regime
    n_samples: int
    pearson_r: float
    spearman_rho: float
    p_value: float
    hit_rate_above_median: float
    hit_rate_below_median: float
    information_coefficient: float
    is_significant: bool
    # Rolling IC（时序稳定性）
    rolling_ic: Optional[RollingICResult] = None
    # 排列检验 p-value（null distribution 下的显著性）
    permutation_p_value: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "indicator": f"{self.indicator_name}.{self.field_name}",
            "forward_bars": self.forward_bars,
            "regime": self.regime,
            "n_samples": self.n_samples,
            "pearson_r": round(self.pearson_r, 4),
            "spearman_rho": round(self.spearman_rho, 4),
            "p_value": round(self.p_value, 4),
            "hit_rate_above_median": round(self.hit_rate_above_median, 4),
            "hit_rate_below_median": round(self.hit_rate_below_median, 4),
            "ic": round(self.information_coefficient, 4),
            "is_significant": self.is_significant,
        }
        if self.rolling_ic is not None:
            d["rolling_ic"] = self.rolling_ic.to_dict()
        if self.permutation_p_value is not None:
            d["permutation_p_value"] = round(self.permutation_p_value, 4)
        return d


# ── Threshold Sweep ─────────────────────────────────────────────


@dataclass(frozen=True)
class ThresholdPoint:
    """单个阈值点的扫描结果。"""

    threshold: float
    direction: str  # "buy_below" | "sell_above"
    hit_rate: float
    mean_return: float
    n_signals: int
    sharpe: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "threshold": round(self.threshold, 4),
            "direction": self.direction,
            "hit_rate": round(self.hit_rate, 4),
            "mean_return": round(self.mean_return, 6),
            "n_signals": self.n_signals,
            "sharpe": round(self.sharpe, 4),
        }


@dataclass(frozen=True)
class ThresholdSweepResult:
    """单个指标字段的阈值扫描结果。"""

    indicator_name: str
    field_name: str
    forward_bars: int
    regime: Optional[str]

    optimal_buy_threshold: Optional[float]
    buy_hit_rate: float
    buy_mean_return: float
    buy_n_signals: int

    optimal_sell_threshold: Optional[float]
    sell_hit_rate: float
    sell_mean_return: float
    sell_n_signals: int

    sweep_points: List[ThresholdPoint] = field(default_factory=list)
    cv_consistency_buy: float = 0.0
    cv_consistency_sell: float = 0.0

    # 在测试集上的验证结果
    test_buy_hit_rate: Optional[float] = None
    test_sell_hit_rate: Optional[float] = None
    test_buy_n_signals: int = 0
    test_sell_n_signals: int = 0

    # 排列检验显著性
    is_significant_buy: bool = False
    is_significant_sell: bool = False
    permutation_p_buy: Optional[float] = None
    permutation_p_sell: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "indicator": f"{self.indicator_name}.{self.field_name}",
            "forward_bars": self.forward_bars,
            "regime": self.regime,
            "buy": {
                "optimal_threshold": (
                    round(self.optimal_buy_threshold, 4)
                    if self.optimal_buy_threshold is not None
                    else None
                ),
                "hit_rate": round(self.buy_hit_rate, 4),
                "mean_return": round(self.buy_mean_return, 6),
                "n_signals": self.buy_n_signals,
                "cv_consistency": round(self.cv_consistency_buy, 2),
                "test_hit_rate": (
                    round(self.test_buy_hit_rate, 4)
                    if self.test_buy_hit_rate is not None
                    else None
                ),
                "test_n_signals": self.test_buy_n_signals,
                "is_significant": self.is_significant_buy,
                "permutation_p": self.permutation_p_buy,
            },
            "sell": {
                "optimal_threshold": (
                    round(self.optimal_sell_threshold, 4)
                    if self.optimal_sell_threshold is not None
                    else None
                ),
                "hit_rate": round(self.sell_hit_rate, 4),
                "mean_return": round(self.sell_mean_return, 6),
                "n_signals": self.sell_n_signals,
                "cv_consistency": round(self.cv_consistency_sell, 2),
                "test_hit_rate": (
                    round(self.test_sell_hit_rate, 4)
                    if self.test_sell_hit_rate is not None
                    else None
                ),
                "test_n_signals": self.test_sell_n_signals,
                "is_significant": self.is_significant_sell,
                "permutation_p": self.permutation_p_sell,
            },
        }


# ── Finding (排名后的发现) ───────────────────────────────────────


@dataclass(frozen=True)
class Finding:
    """一个可操作的发现，按显著性排名。"""

    rank: int
    category: str  # "predictive_power" | "threshold" | "regime_affinity"
    summary: str
    confidence_level: str  # "high" | "medium" | "low"
    significance_score: float
    action: str
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rank": self.rank,
            "category": self.category,
            "summary": self.summary,
            "confidence": self.confidence_level,
            "significance_score": round(self.significance_score, 4),
            "action": self.action,
        }


# ── 顶层结果容器 ────────────────────────────────────────────────


@dataclass
class MiningResult:
    """完整挖掘运行结果。"""

    run_id: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    data_summary: Optional[DataSummary] = None
    experiment_id: Optional[str] = None

    predictive_power: List[IndicatorPredictiveResult] = field(default_factory=list)
    threshold_sweeps: List[ThresholdSweepResult] = field(default_factory=list)
    mined_rules: List[Any] = field(default_factory=list)  # List[MinedRule]

    top_findings: List[Finding] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "run_id": self.run_id,
            "experiment_id": self.experiment_id,
            "started_at": self.started_at.isoformat(),
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
        }
        if self.data_summary:
            result["data_summary"] = self.data_summary.to_dict()
        if self.predictive_power:
            result["predictive_power"] = [r.to_dict() for r in self.predictive_power]
        if self.threshold_sweeps:
            result["threshold_sweeps"] = [r.to_dict() for r in self.threshold_sweeps]
        if self.mined_rules:
            result["mined_rules"] = [r.to_dict() for r in self.mined_rules]
        if self.top_findings:
            result["top_findings"] = [f.to_dict() for f in self.top_findings]
        return result
