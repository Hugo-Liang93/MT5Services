"""信号挖掘结果模型。

遵循项目惯例：内部结果用 frozen dataclass + to_dict()，
与 BacktestMetrics / MonteCarloResult / CorrelationAnalysis 一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
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
    information_ratio: float  # mean_ic / std_ic_corrected, IR > 0.5 = 稳定
    n_windows: int
    # 校正后的有效独立窗口数（考虑窗口重叠）
    effective_n_windows: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mean_ic": round(self.mean_ic, 4),
            "std_ic": round(self.std_ic, 4),
            "information_ratio": round(self.information_ratio, 4),
            "n_windows": self.n_windows,
            "effective_n_windows": round(self.effective_n_windows, 1),
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
    # 统计效力（L6）：给定样本量可检测的最小 IC
    min_detectable_ic: Optional[float] = None

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
        if self.min_detectable_ic is not None:
            d["min_detectable_ic"] = round(self.min_detectable_ic, 4)
        return d


# ── Barrier Predictive Power ────────────────────────────────────


@dataclass(frozen=True)
class IndicatorBarrierPredictiveResult:
    """单个 (indicator_field, barrier_config, direction, regime) 的 barrier IC 结果。

    替代 IndicatorPredictiveResult 的短 forward_return 语义——y 是 Triple-Barrier
    模拟的真实 tp/sl/time 出场收益，与实盘 Chandelier exit 模型同构。

    关键字段：
      - information_coefficient：指标值与 barrier 真实收益的 Spearman ρ
      - tp/sl/time_exit_rate：barrier 触发分布（反映该配置的真实持仓表现）
      - mean_bars_held：平均持仓 bar 数（检验 barrier 与期望 exit 长度匹配度）
    """

    indicator_name: str
    field_name: str
    regime: Optional[str]  # None = 全部 regime
    direction: str  # "long" | "short"
    barrier_key: tuple  # (sl_atr: float, tp_atr: float, time_bars: int)

    n_samples: int
    pearson_r: float
    spearman_rho: float
    information_coefficient: float  # = spearman_rho，保持与 IndicatorPredictiveResult 同名
    p_value: float
    permutation_p_value: Optional[float]
    is_significant: bool

    # Barrier exit breakdown
    tp_hit_rate: float
    sl_hit_rate: float
    time_exit_rate: float
    mean_bars_held: float
    mean_return_pct: float  # 扣 cost 后

    def to_dict(self) -> Dict[str, Any]:
        sl_atr, tp_atr, time_bars = self.barrier_key
        return {
            "indicator": f"{self.indicator_name}.{self.field_name}",
            "regime": self.regime,
            "direction": self.direction,
            "barrier": f"sl={sl_atr}/tp={tp_atr}/time={time_bars}",
            "n_samples": self.n_samples,
            "pearson_r": round(self.pearson_r, 4),
            "spearman_rho": round(self.spearman_rho, 4),
            "ic": round(self.information_coefficient, 4),
            "p_value": round(self.p_value, 4),
            "permutation_p_value": (
                round(self.permutation_p_value, 4)
                if self.permutation_p_value is not None
                else None
            ),
            "is_significant": self.is_significant,
            "tp_hit_rate": round(self.tp_hit_rate, 4),
            "sl_hit_rate": round(self.sl_hit_rate, 4),
            "time_exit_rate": round(self.time_exit_rate, 4),
            "mean_bars_held": round(self.mean_bars_held, 2),
            "mean_return_pct": round(self.mean_return_pct, 4),
        }


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
    # 盈亏比分解（区分胜率型 vs 盈亏比型策略）
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0  # total_wins / total_losses

    def to_dict(self) -> Dict[str, Any]:
        return {
            "threshold": round(self.threshold, 4),
            "direction": self.direction,
            "hit_rate": round(self.hit_rate, 4),
            "mean_return": round(self.mean_return, 6),
            "n_signals": self.n_signals,
            "sharpe": round(self.sharpe, 4),
            "avg_win": round(self.avg_win, 6),
            "avg_loss": round(self.avg_loss, 6),
            "profit_factor": round(self.profit_factor, 2),
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


class RobustnessTier(str, Enum):
    ROBUST = "robust"
    TF_SPECIFIC = "tf_specific"
    DIVERGENT = "divergent"


class PromotionDecision(str, Enum):
    REJECT = "reject"
    REFIT = "refit"
    PAPER_ONLY = "paper_only"
    ACTIVE_GUARDED = "active_guarded"
    ACTIVE = "active"


class IndicatorPromotionDecision(str, Enum):
    REJECT = "reject"
    REFIT = "refit"
    RESEARCH_ONLY = "research_only"
    PROMOTE_INDICATOR = "promote_indicator"
    PROMOTE_INDICATOR_AND_STRATEGY_CANDIDATE = (
        "promote_indicator_and_strategy_candidate"
    )


class FeatureKind(str, Enum):
    """特征类型分类（ADR-007）。

    DERIVED  — 组合型：现有指标字段的条件组合（如 body_ratio、momentum_consensus）
               策略可通过现有指标字段直接引用，无需晋升
    COMPUTED — 计算型：需独立 Python 函数实现的新指标（如 regime_entropy、intrabar 衍生量）
               晋升需 4 步：写 src/indicators/core/、加 indicators.json、写测试、触发 pipeline
    """

    DERIVED = "derived"
    COMPUTED = "computed"


@dataclass(frozen=True)
class CandidateRuleCondition:
    role: str  # why | when | where
    indicator: str
    field: str
    operator: str
    threshold: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "indicator": self.indicator,
            "field": self.field,
            "operator": self.operator,
            "threshold": round(self.threshold, 4),
        }


@dataclass(frozen=True)
class CandidateEvidence:
    evidence_type: str  # predictive_power | threshold | rule_mining
    timeframe: str
    summary: str
    direction: Optional[str] = None
    regime: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evidence_type": self.evidence_type,
            "timeframe": self.timeframe,
            "summary": self.summary,
            "direction": self.direction,
            "regime": self.regime,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class FeatureCandidateSpec:
    candidate_id: str
    symbol: str
    target_timeframe: str
    allowed_timeframes: Tuple[str, ...]
    feature_name: str
    formula_summary: str
    dependencies: Tuple[str, ...]
    runtime_state_inputs: Tuple[str, ...]
    live_computable: bool
    compute_scope: str
    robustness_tier: RobustnessTier
    direction_hint: Optional[str]
    strategy_roles: Tuple[str, ...]
    evidence: Tuple[CandidateEvidence, ...] = field(default_factory=tuple)
    validation_gates: Dict[str, Any] = field(default_factory=dict)
    promotion_decision: IndicatorPromotionDecision = (
        IndicatorPromotionDecision.RESEARCH_ONLY
    )
    feature_kind: FeatureKind = FeatureKind.DERIVED
    research_provenance: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "symbol": self.symbol,
            "target_timeframe": self.target_timeframe,
            "allowed_timeframes": list(self.allowed_timeframes),
            "feature_name": self.feature_name,
            "formula_summary": self.formula_summary,
            "dependencies": list(self.dependencies),
            "runtime_state_inputs": list(self.runtime_state_inputs),
            "live_computable": self.live_computable,
            "compute_scope": self.compute_scope,
            "robustness_tier": self.robustness_tier.value,
            "direction_hint": self.direction_hint,
            "strategy_roles": list(self.strategy_roles),
            "evidence": [item.to_dict() for item in self.evidence],
            "validation_gates": dict(self.validation_gates),
            "promotion_decision": self.promotion_decision.value,
            "feature_kind": self.feature_kind.value,
            "research_provenance": self.research_provenance,
        }


@dataclass(frozen=True)
class StrategyCandidateSpec:
    candidate_id: str
    symbol: str
    target_timeframe: str
    allowed_timeframes: Tuple[str, ...]
    source_run_ids: Tuple[str, ...]
    robustness_tier: RobustnessTier
    promotion_decision: PromotionDecision
    key_indicator: str
    regime: Optional[str]
    direction: Optional[str]
    why_conditions: Tuple[CandidateRuleCondition, ...] = field(default_factory=tuple)
    when_conditions: Tuple[CandidateRuleCondition, ...] = field(default_factory=tuple)
    where_conditions: Tuple[CandidateRuleCondition, ...] = field(default_factory=tuple)
    thresholds: Dict[str, float] = field(default_factory=dict)
    dominant_sessions: Tuple[str, ...] = field(default_factory=tuple)
    evidence_types: Tuple[str, ...] = field(default_factory=tuple)
    evidence: Tuple[CandidateEvidence, ...] = field(default_factory=tuple)
    validation_gates: Dict[str, Any] = field(default_factory=dict)
    decision_reason: str = ""
    research_provenance: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "symbol": self.symbol,
            "target_timeframe": self.target_timeframe,
            "allowed_timeframes": list(self.allowed_timeframes),
            "source_run_ids": list(self.source_run_ids),
            "robustness_tier": self.robustness_tier.value,
            "promotion_decision": self.promotion_decision.value,
            "key_indicator": self.key_indicator,
            "regime": self.regime,
            "direction": self.direction,
            "why_conditions": [item.to_dict() for item in self.why_conditions],
            "when_conditions": [item.to_dict() for item in self.when_conditions],
            "where_conditions": [item.to_dict() for item in self.where_conditions],
            "thresholds": {
                key: round(value, 4) for key, value in self.thresholds.items()
            },
            "dominant_sessions": list(self.dominant_sessions),
            "evidence_types": list(self.evidence_types),
            "evidence": [item.to_dict() for item in self.evidence],
            "validation_gates": dict(self.validation_gates),
            "decision_reason": self.decision_reason,
            "research_provenance": self.research_provenance,
        }


@dataclass(frozen=True)
class CandidateDiscoveryResult:
    symbol: str
    timeframes: Tuple[str, ...]
    candidate_specs: Tuple[StrategyCandidateSpec, ...]
    cross_tf_analysis: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframes": list(self.timeframes),
            "candidate_specs": [spec.to_dict() for spec in self.candidate_specs],
            "cross_tf_analysis": dict(self.cross_tf_analysis),
        }


@dataclass(frozen=True)
class FeatureCandidateDiscoveryResult:
    symbol: str
    timeframes: Tuple[str, ...]
    feature_candidates: Tuple[FeatureCandidateSpec, ...]
    cross_tf_analysis: Dict[str, Any] = field(default_factory=dict)
    registry_inventory: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframes": list(self.timeframes),
            "feature_candidates": [spec.to_dict() for spec in self.feature_candidates],
            "cross_tf_analysis": dict(self.cross_tf_analysis),
            "registry_inventory": dict(self.registry_inventory),
        }


@dataclass(frozen=True)
class FeaturePromotionReport:
    candidate_id: str
    feature_name: str
    promoted_indicator_name: Optional[str]
    promotion_decision: IndicatorPromotionDecision
    strategy_candidates: Tuple[str, ...] = field(default_factory=tuple)
    validation_summary: Dict[str, Any] = field(default_factory=dict)
    research_provenance: Optional[str] = None
    lineage: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "feature_name": self.feature_name,
            "promoted_indicator_name": self.promoted_indicator_name,
            "promotion_decision": self.promotion_decision.value,
            "strategy_candidates": list(self.strategy_candidates),
            "validation_summary": dict(self.validation_summary),
            "research_provenance": self.research_provenance,
            "lineage": dict(self.lineage),
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
    barrier_predictive_power: List[IndicatorBarrierPredictiveResult] = field(
        default_factory=list
    )

    top_findings: List[Finding] = field(default_factory=list)

    # FeatureHub 集成（Task 12）
    feature_compute_summary: Optional[Dict[str, Any]] = None
    findings_by_provider: Dict[str, List[Finding]] = field(default_factory=dict)
    cross_provider_rules: List[Any] = field(default_factory=list)

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
        if self.barrier_predictive_power:
            result["barrier_predictive_power"] = [
                r.to_dict() for r in self.barrier_predictive_power
            ]
        if self.top_findings:
            result["top_findings"] = [f.to_dict() for f in self.top_findings]
        if self.feature_compute_summary:
            result["feature_compute_summary"] = self.feature_compute_summary
        if self.findings_by_provider:
            result["findings_by_provider"] = {
                prov: [f.to_dict() for f in findings]
                for prov, findings in self.findings_by_provider.items()
            }
        if self.cross_provider_rules:
            result["cross_provider_rules"] = [
                r.to_dict() for r in self.cross_provider_rules
            ]
        return result
