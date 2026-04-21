"""回测数据模型定义。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class BacktestJobStatus(str, Enum):
    """回测任务生命周期状态。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class SimulationMode(str, Enum):
    """回测执行语义。

    research:
        研究模式。共享实盘指标/策略/过滤链路，但不强制执行最小手数可成交性；
        允许理论上的小数仓位，用于策略研究、挖掘和参数搜索。

    execution_feasibility:
        可执行性模拟。共享同一套信号内核，但会校验最小手数等执行约束，
        用于回答“按当前资金规模/执行约束，这个信号是否能成交”。
    """

    RESEARCH = "research"
    EXECUTION_FEASIBILITY = "execution_feasibility"


class ValidationDecision(str, Enum):
    REJECT = "reject"
    REFIT = "refit"
    PAPER_ONLY = "paper_only"
    ACTIVE_GUARDED = "active_guarded"
    ACTIVE = "active"


@dataclass
class BacktestJob:
    """回测任务元数据（submit/query job 模式的核心数据结构）。

    提交时立即持久化到 DB，运行过程中更新状态。
    API 重启后仍可查询历史任务状态。
    """

    run_id: str
    job_type: str  # "backtest" | "optimization"
    status: BacktestJobStatus
    submitted_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    # 进度透出（0.0 ~ 1.0）
    progress: float = 0.0
    # 配置摘要（用于 list 展示）
    config_summary: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "job_type": self.job_type,
            "status": self.status.value,
            "submitted_at": self.submitted_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
            "error": self.error,
            "progress": round(self.progress, 3),
            "config_summary": self.config_summary,
        }


# ── 回测子配置 ────────────────────────────────────────────────────


@dataclass(frozen=True)
class PositionConfig:
    """持仓管理配置（sizing / trailing / breakeven / EOD / regime sizing）。"""

    contract_size: float = 100.0
    risk_percent: float = 1.0
    min_volume: float = 0.01
    max_volume: float = 1.0
    trailing_atr_multiplier: float = 0.8
    breakeven_atr_threshold: float = 0.8
    end_of_day_close_enabled: bool = False
    end_of_day_close_hour_utc: int = 21
    end_of_day_close_minute_utc: int = 0
    # Regime 感知 SL/TP 缩放
    regime_tp_trending: float = 1.30
    regime_sl_trending: float = 1.10
    regime_tp_ranging: float = 0.75
    regime_sl_ranging: float = 0.85
    regime_tp_breakout: float = 1.20
    regime_sl_breakout: float = 1.10
    regime_tp_uncertain: float = 1.00
    regime_sl_uncertain: float = 1.00


@dataclass(frozen=True)
class RiskConfig:
    """风控约束配置（仓位限制 / 手数限制 / 频率限制 / 成本）。"""

    max_positions: int = 3
    commission_per_lot: float = 0.0
    slippage_points: float = 0.0
    max_volume_per_order: Optional[float] = None
    max_volume_per_symbol: Optional[float] = None
    max_volume_per_day: Optional[float] = None
    daily_loss_limit_pct: Optional[float] = None
    max_trades_per_day: Optional[int] = None
    max_trades_per_hour: Optional[int] = None
    # 动态 spread 建模（XAUUSD 时段差异大：亚盘 ~15pt / 欧盘 ~18pt / 美盘 ~22pt / 高波动 ×2+）
    # 启用后，开平仓各扣一半 spread，并可叠加 slippage_points 作为非 spread 类滑点
    dynamic_spread_enabled: bool = False
    spread_base_points: float = 15.0
    spread_london_mult: float = 1.2
    spread_ny_mult: float = 1.3
    spread_asia_mult: float = 1.0
    # 高波动放大：当前 ATR / 参考 ATR 超阈值则 spread × spread_volatility_mult
    spread_volatility_threshold: float = 1.8
    spread_volatility_mult: float = 2.0
    # Overnight swap（XAUUSD 每 lot 每日 USD，过夜日 UTC 21:00 触发）
    swap_enabled: bool = False
    swap_long_per_lot: float = -0.3
    swap_short_per_lot: float = 0.15
    swap_wednesday_triple: bool = True
    swap_charge_hour_utc: int = 21


@dataclass(frozen=True)
class FilterConfig:
    """过滤器配置（模拟实盘 SignalFilterChain）。"""

    enabled: bool = True
    session_enabled: bool = True
    allowed_sessions: str = "london,new_york"
    session_transition_enabled: bool = True
    session_transition_cooldown: int = 15
    volatility_enabled: bool = True
    volatility_spike_multiplier: float = 2.5
    economic_enabled: bool = True
    economic_lookahead_minutes: int = 30
    economic_lookback_minutes: int = 15
    economic_importance_min: int = 2
    spread_enabled: bool = False
    max_spread_points: float = 50.0


@dataclass(frozen=True)
class PendingEntryConfig:
    """Pending Entry 价格确认入场配置。"""

    enabled: bool = False
    pullback_atr_factor: float = 0.3
    chase_atr_factor: float = 0.1
    momentum_atr_factor: float = 0.5
    symmetric_atr_factor: float = 0.4
    expiry_bars: int = 2


@dataclass(frozen=True)
class TrailingTPConfig:
    """Trailing Take Profit 配置。"""

    enabled: bool = False
    activation_atr: float = 1.5
    trail_atr: float = 0.8


@dataclass(frozen=True)
class CircuitBreakerConfig:
    """连败熔断器配置（基于 bar 计数）。"""

    enabled: bool = False
    max_consecutive_losses: int = 5
    cooldown_bars: int = 20


@dataclass(frozen=True)
class MonteCarloConfig:
    """蒙特卡洛排列检验配置。"""

    enabled: bool = False
    simulations: int = 1000
    confidence_level: float = 0.95
    seed: Optional[int] = None


@dataclass(frozen=True)
class ConfidenceConfig:
    """置信度管线配置。"""

    min_confidence: float = 0.55
    enable_regime_affinity: bool = True
    enable_performance_tracker: bool = True
    enable_calibrator: bool = True
    enable_htf_alignment: bool = True
    htf_alignment_boost: float = 1.10
    htf_conflict_penalty: float = 0.70
    bars_to_evaluate: int = 5


@dataclass(frozen=True)
class IntrabarConfig:
    """Intrabar 回测配置（双 TF 联合回放）。

    与生产 signal.ini [intrabar_trading] 对齐，复用同一语义：
    - trigger_map: 父 TF → 子 TF（e.g. {"H1": "M5", "M30": "M1"}）
    - min_stable_bars / min_confidence: 直接传给 IntrabarTradeCoordinator
    - enabled_strategies: 策略白名单（空=全部支持 intrabar scope 的策略）
    - confidence_factor: intrabar 置信度衰减系数（与生产 intrabar_confidence_factor 相同）
    """

    enabled: bool = False
    trigger_map: Dict[str, str] = field(default_factory=dict)
    min_stable_bars: int = 3
    min_confidence: float = 0.75
    enabled_strategies: List[str] = field(default_factory=list)
    confidence_factor: float = 0.85
    # 子 TF 数据覆盖率低于此阈值时跳过该父 bar 的 intrabar 评估
    min_coverage_ratio: float = 0.80


# ── 字段名映射（flat key -> (sub_config_field, nested_key)）──────────


_FLAT_FIELD_MAP: Dict[str, Tuple[str, str]] = {
    # PositionConfig
    "contract_size": ("position", "contract_size"),
    "risk_percent": ("position", "risk_percent"),
    "min_volume": ("position", "min_volume"),
    "max_volume": ("position", "max_volume"),
    "trailing_atr_multiplier": ("position", "trailing_atr_multiplier"),
    "breakeven_atr_threshold": ("position", "breakeven_atr_threshold"),
    "end_of_day_close_enabled": ("position", "end_of_day_close_enabled"),
    "end_of_day_close_hour_utc": ("position", "end_of_day_close_hour_utc"),
    "end_of_day_close_minute_utc": ("position", "end_of_day_close_minute_utc"),
    "regime_tp_trending": ("position", "regime_tp_trending"),
    "regime_sl_trending": ("position", "regime_sl_trending"),
    "regime_tp_ranging": ("position", "regime_tp_ranging"),
    "regime_sl_ranging": ("position", "regime_sl_ranging"),
    "regime_tp_breakout": ("position", "regime_tp_breakout"),
    "regime_sl_breakout": ("position", "regime_sl_breakout"),
    "regime_tp_uncertain": ("position", "regime_tp_uncertain"),
    "regime_sl_uncertain": ("position", "regime_sl_uncertain"),
    # RiskConfig
    "max_positions": ("risk", "max_positions"),
    "commission_per_lot": ("risk", "commission_per_lot"),
    "slippage_points": ("risk", "slippage_points"),
    "max_volume_per_order": ("risk", "max_volume_per_order"),
    "max_volume_per_symbol": ("risk", "max_volume_per_symbol"),
    "max_volume_per_day": ("risk", "max_volume_per_day"),
    "daily_loss_limit_pct": ("risk", "daily_loss_limit_pct"),
    "max_trades_per_day": ("risk", "max_trades_per_day"),
    "max_trades_per_hour": ("risk", "max_trades_per_hour"),
    "dynamic_spread_enabled": ("risk", "dynamic_spread_enabled"),
    "spread_base_points": ("risk", "spread_base_points"),
    "spread_london_mult": ("risk", "spread_london_mult"),
    "spread_ny_mult": ("risk", "spread_ny_mult"),
    "spread_asia_mult": ("risk", "spread_asia_mult"),
    "spread_volatility_threshold": ("risk", "spread_volatility_threshold"),
    "spread_volatility_mult": ("risk", "spread_volatility_mult"),
    "swap_enabled": ("risk", "swap_enabled"),
    "swap_long_per_lot": ("risk", "swap_long_per_lot"),
    "swap_short_per_lot": ("risk", "swap_short_per_lot"),
    "swap_wednesday_triple": ("risk", "swap_wednesday_triple"),
    "swap_charge_hour_utc": ("risk", "swap_charge_hour_utc"),
    # FilterConfig
    "filters_enabled": ("filters", "enabled"),
    "filter_session_enabled": ("filters", "session_enabled"),
    "filter_allowed_sessions": ("filters", "allowed_sessions"),
    "filter_session_transition_enabled": ("filters", "session_transition_enabled"),
    "filter_session_transition_cooldown": ("filters", "session_transition_cooldown"),
    "filter_volatility_enabled": ("filters", "volatility_enabled"),
    "filter_volatility_spike_multiplier": ("filters", "volatility_spike_multiplier"),
    "filter_economic_enabled": ("filters", "economic_enabled"),
    "filter_economic_lookahead_minutes": ("filters", "economic_lookahead_minutes"),
    "filter_economic_lookback_minutes": ("filters", "economic_lookback_minutes"),
    "filter_economic_importance_min": ("filters", "economic_importance_min"),
    "filter_spread_enabled": ("filters", "spread_enabled"),
    "filter_max_spread_points": ("filters", "max_spread_points"),
    # PendingEntryConfig
    "pending_entry_enabled": ("pending_entry", "enabled"),
    "pending_entry_pullback_atr_factor": ("pending_entry", "pullback_atr_factor"),
    "pending_entry_chase_atr_factor": ("pending_entry", "chase_atr_factor"),
    "pending_entry_momentum_atr_factor": ("pending_entry", "momentum_atr_factor"),
    "pending_entry_symmetric_atr_factor": ("pending_entry", "symmetric_atr_factor"),
    "pending_entry_expiry_bars": ("pending_entry", "expiry_bars"),
    # TrailingTPConfig
    "trailing_tp_enabled": ("trailing_tp", "enabled"),
    "trailing_tp_activation_atr": ("trailing_tp", "activation_atr"),
    "trailing_tp_trail_atr": ("trailing_tp", "trail_atr"),
    # CircuitBreakerConfig
    "circuit_breaker_enabled": ("circuit_breaker", "enabled"),
    "circuit_breaker_max_consecutive_losses": (
        "circuit_breaker",
        "max_consecutive_losses",
    ),
    "circuit_breaker_cooldown_bars": ("circuit_breaker", "cooldown_bars"),
    # MonteCarloConfig
    "monte_carlo_enabled": ("monte_carlo", "enabled"),
    "monte_carlo_simulations": ("monte_carlo", "simulations"),
    "monte_carlo_confidence_level": ("monte_carlo", "confidence_level"),
    "monte_carlo_seed": ("monte_carlo", "seed"),
    # ConfidenceConfig
    "min_confidence": ("confidence", "min_confidence"),
    "enable_regime_affinity": ("confidence", "enable_regime_affinity"),
    "enable_performance_tracker": ("confidence", "enable_performance_tracker"),
    "enable_calibrator": ("confidence", "enable_calibrator"),
    "enable_htf_alignment": ("confidence", "enable_htf_alignment"),
    "htf_alignment_boost": ("confidence", "htf_alignment_boost"),
    "htf_conflict_penalty": ("confidence", "htf_conflict_penalty"),
    "bars_to_evaluate": ("confidence", "bars_to_evaluate"),
    # IntrabarConfig
    "intrabar_enabled": ("intrabar", "enabled"),
    "intrabar_min_stable_bars": ("intrabar", "min_stable_bars"),
    "intrabar_min_confidence": ("intrabar", "min_confidence"),
    "intrabar_confidence_factor": ("intrabar", "confidence_factor"),
    "intrabar_min_coverage_ratio": ("intrabar", "min_coverage_ratio"),
}

_SUB_CONFIG_CLASSES: Dict[str, type] = {
    "position": PositionConfig,
    "risk": RiskConfig,
    "filters": FilterConfig,
    "pending_entry": PendingEntryConfig,
    "trailing_tp": TrailingTPConfig,
    "circuit_breaker": CircuitBreakerConfig,
    "monte_carlo": MonteCarloConfig,
    "confidence": ConfidenceConfig,
    "intrabar": IntrabarConfig,
}


@dataclass(frozen=True)
class BacktestConfig:
    """回测运行配置。"""

    # ── 核心参数（不属于任何子配置）────────────────────────────────
    symbol: str
    timeframe: str
    start_time: datetime
    end_time: datetime
    strategies: Optional[List[str]] = None
    initial_balance: float = 10000.0
    simulation_mode: SimulationMode = SimulationMode.RESEARCH
    strategy_params: Dict[str, Any] = field(default_factory=dict)
    strategy_params_per_tf: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    regime_affinity_overrides: Dict[str, Dict[str, float]] = field(default_factory=dict)
    strategy_timeframes: Dict[str, List[str]] = field(default_factory=dict)
    strategy_sessions: Dict[str, List[str]] = field(default_factory=dict)
    warmup_bars: int = 200
    enable_state_machine: bool = False
    min_preview_stable_bars: int = 1
    max_signal_evaluations: int = 50000

    # ── 嵌套子配置 ────────────────────────────────────────────────
    position: PositionConfig = field(default_factory=PositionConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    pending_entry: PendingEntryConfig = field(default_factory=PendingEntryConfig)
    trailing_tp: TrailingTPConfig = field(default_factory=TrailingTPConfig)
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    monte_carlo: MonteCarloConfig = field(default_factory=MonteCarloConfig)
    confidence: ConfidenceConfig = field(default_factory=ConfidenceConfig)
    intrabar: IntrabarConfig = field(default_factory=IntrabarConfig)

    @classmethod
    def from_flat(cls, **kwargs: Any) -> "BacktestConfig":
        """从平铺的字段字典构建 BacktestConfig（向后兼容）。

        接受旧式平铺字段名（如 ``filter_session_enabled``），
        自动归类到对应子配置中。
        """
        sub_buckets: Dict[str, Dict[str, Any]] = {}
        core_kwargs: Dict[str, Any] = {}

        for key, value in kwargs.items():
            mapping = _FLAT_FIELD_MAP.get(key)
            if mapping is not None:
                sub_name, nested_key = mapping
                sub_buckets.setdefault(sub_name, {})[nested_key] = value
            else:
                core_kwargs[key] = value

        simulation_mode = core_kwargs.get("simulation_mode")
        if simulation_mode is not None and not isinstance(
            simulation_mode, SimulationMode
        ):
            core_kwargs["simulation_mode"] = SimulationMode(str(simulation_mode))

        for sub_name, sub_kwargs in sub_buckets.items():
            core_kwargs[sub_name] = _SUB_CONFIG_CLASSES[sub_name](**sub_kwargs)

        return cls(**core_kwargs)


@dataclass(frozen=True)
class TradeRecord:
    """单笔回测交易记录。"""

    signal_id: str
    strategy: str
    direction: str  # "buy" | "sell"
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    stop_loss: float
    take_profit: float
    position_size: float
    pnl: float  # 盈亏金额
    pnl_pct: float  # 盈亏百分比
    bars_held: int
    regime: str
    confidence: float
    exit_reason: str  # "take_profit" | "stop_loss" | "signal_exit" | "end_of_test"
    entry_scope: str = "confirmed"  # "confirmed" | "intrabar"
    slippage_cost: float = 0.0  # 开仓+平仓滑点/spread 总成本（USD）
    commission_cost: float = 0.0  # 手续费总成本
    swap_cost: float = 0.0  # 过夜利息累计（USD，负数表示支付）
    # P11 Phase 4a: 交易结构字段
    # MFE (Maximum Favorable Excursion) — 持仓期间最大顺势浮盈（百分比）
    # MAE (Maximum Adverse Excursion) — 持仓期间最大逆势浮亏（百分比，正数）
    mfe_pct: Optional[float] = None
    mae_pct: Optional[float] = None
    hold_minutes: Optional[int] = None  # 持仓分钟数（exit_time - entry_time）


@dataclass(frozen=True)
class SignalEvaluation:
    """单次信号评估记录（无论是否开仓）。

    对应实盘的 signal_outcomes 表，用于回测中跟踪
    每个策略在每根 bar 上的评估结果和后续价格变化。
    """

    bar_time: datetime
    strategy: str
    direction: str  # "buy" | "sell" | "hold"
    confidence: float
    regime: str
    # N bars 后的价格变化（回填）
    price_at_signal: float
    price_after_n_bars: Optional[float] = None
    bars_to_evaluate: int = 5
    won: Optional[bool] = None
    pnl_pct: Optional[float] = None
    # 是否被过滤器拒绝（记录原因）
    filtered: bool = False
    filter_reason: str = ""
    # 回测结束时未满 N bars 的标记（价格回填不完整，统计时应区分对待）
    incomplete: bool = False


@dataclass(frozen=True)
class BacktestMetrics:
    """回测统计指标。"""

    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    expectancy: float
    profit_factor: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown: float
    max_drawdown_duration: int  # bar 数
    avg_win: float
    avg_loss: float
    avg_bars_held: float
    total_pnl: float
    total_pnl_pct: float
    calmar_ratio: float  # Return / Max Drawdown
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0


@dataclass(frozen=True)
class ValidationDecisionReport:
    decision: ValidationDecision
    robustness_tier: Optional[str]
    checks: Dict[str, Dict[str, Any]]
    reasons: List[str]
    feature_candidate_id: Optional[str] = None
    promoted_indicator_name: Optional[str] = None
    strategy_candidate_id: Optional[str] = None
    research_provenance: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision.value,
            "robustness_tier": self.robustness_tier,
            "checks": self.checks,
            "reasons": list(self.reasons),
            "feature_candidate_id": self.feature_candidate_id,
            "promoted_indicator_name": self.promoted_indicator_name,
            "strategy_candidate_id": self.strategy_candidate_id,
            "research_provenance": self.research_provenance,
        }


@dataclass
class BacktestResult:
    """单次回测运行结果。"""

    config: BacktestConfig
    run_id: str
    started_at: datetime
    completed_at: datetime
    trades: List[TradeRecord]
    equity_curve: List[Tuple[datetime, float]]  # (time, balance)
    metrics: BacktestMetrics
    metrics_by_regime: Dict[str, BacktestMetrics]
    metrics_by_strategy: Dict[str, BacktestMetrics]
    metrics_by_confidence: Dict[str, BacktestMetrics]  # high/medium/low
    param_set: Dict[str, Any]
    # 过滤器统计
    filter_stats: Optional[Dict[str, Any]] = None
    # 策略能力执行计划（与实盘 runtime status 同语义）
    strategy_capability_execution_plan: Optional[Dict[str, Any]] = None
    # 执行语义摘要（用于区分 research 与 execution_feasibility）
    execution_summary: Optional[Dict[str, Any]] = None
    # 信号评估明细（用于回测质量分析）
    signal_evaluations: Optional[List[SignalEvaluation]] = None
    # 蒙特卡洛排列检验结果
    monte_carlo_result: Optional[Dict[str, Any]] = None
    # 晋升验证裁决（由独立 validation 组合回测/WF/paper 结果后填充）
    validation_decision: Optional[ValidationDecisionReport] = None
    # 实验追踪 ID（跨 Research/Backtest/PaperTrading 关联）
    experiment_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可 JSON 化的字典。"""
        from dataclasses import asdict

        result = asdict(self)
        # datetime 转字符串
        result["started_at"] = self.started_at.isoformat()
        result["completed_at"] = self.completed_at.isoformat()
        result["config"]["start_time"] = self.config.start_time.isoformat()
        result["config"]["end_time"] = self.config.end_time.isoformat()
        result["config"]["simulation_mode"] = self.config.simulation_mode.value
        result["equity_curve"] = [(t.isoformat(), v) for t, v in self.equity_curve]
        for trade in result["trades"]:
            trade["entry_time"] = (
                trade["entry_time"].isoformat()
                if isinstance(trade["entry_time"], datetime)
                else trade["entry_time"]
            )
            trade["exit_time"] = (
                trade["exit_time"].isoformat()
                if isinstance(trade["exit_time"], datetime)
                else trade["exit_time"]
            )
        # 信号评估记录 datetime 转字符串
        if result.get("signal_evaluations"):
            for ev in result["signal_evaluations"]:
                if isinstance(ev.get("bar_time"), datetime):
                    ev["bar_time"] = ev["bar_time"].isoformat()
        if self.validation_decision is not None:
            result["validation_decision"] = self.validation_decision.to_dict()
        return result


@dataclass
class ParameterSpace:
    """参数搜索空间定义。

    支持三类参数搜索：
    - strategy_params: 策略参数（signal.ini [strategy_params] 格式，双下划线）
    - position_params: 持仓管理参数（trailing_atr_multiplier, breakeven_atr_threshold 等）
    - regime_affinity_overrides: Regime 亲和度覆盖
    """

    strategy_params: Dict[str, List[Any]] = field(default_factory=dict)
    position_params: Dict[str, List[Any]] = field(default_factory=dict)
    regime_affinity_overrides: Dict[str, Dict[str, List[float]]] = field(
        default_factory=dict
    )
    search_mode: str = "grid"  # "grid" | "random"
    max_combinations: int = 500  # random 模式下的最大采样数


class RecommendationStatus(str, Enum):
    """参数推荐的生命周期状态。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True)
class ParamRobustness:
    """单个参数的鲁棒性评估结果。"""

    param_key: str  # e.g. "supertrend__adx_threshold"
    base_value: float  # 最优参数值
    base_sharpe: float  # 最优参数对应的 Sharpe
    # 扰动后 Sharpe 的变异系数（标准差/均值），越小越鲁棒
    sharpe_cv: float
    # 扰动后 Sharpe 的最小值
    min_sharpe: float
    # 扰动后最大 Sharpe 降幅百分比（相对 base_sharpe）
    max_degradation_pct: float
    # 是否稳定（sharpe_cv < 0.3 且 max_degradation_pct < 30%）
    is_stable: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "param_key": self.param_key,
            "base_value": self.base_value,
            "base_sharpe": round(self.base_sharpe, 4),
            "sharpe_cv": round(self.sharpe_cv, 4),
            "min_sharpe": round(self.min_sharpe, 4),
            "max_degradation_pct": round(self.max_degradation_pct, 2),
            "is_stable": self.is_stable,
        }


@dataclass
class RobustnessResult:
    """参数鲁棒性检查结果。"""

    best_params: Dict[str, Any]
    best_sharpe: float
    param_robustness: List[ParamRobustness]
    # 所有参数均稳定
    all_stable: bool
    # 不稳定参数列表
    fragile_params: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "best_params": self.best_params,
            "best_sharpe": round(self.best_sharpe, 4),
            "param_robustness": [p.to_dict() for p in self.param_robustness],
            "all_stable": self.all_stable,
            "fragile_params": self.fragile_params,
        }


@dataclass(frozen=True)
class ParamChange:
    """单个参数变更。"""

    section: (
        str  # "strategy_params" | "strategy_params.{TF}" | "regime_affinity.{strategy}"
    )
    key: str  # e.g. "supertrend__adx_threshold"
    old_value: Optional[float]  # None = 新增参数
    new_value: float
    change_pct: float  # 变更幅度百分比（正=增大，负=减小）


@dataclass
class Recommendation:
    """一条参数推荐记录。

    由 Walk-Forward 验证结果自动生成，经人工审核后一键应用。
    """

    rec_id: str  # "rec_{uuid12}"
    source_run_id: str  # Walk-Forward 的 run_id
    created_at: datetime
    status: RecommendationStatus

    # Walk-Forward 验证指标
    overfitting_ratio: float
    consistency_rate: float
    oos_sharpe: float  # OOS 聚合 Sharpe
    oos_win_rate: float  # OOS 聚合胜率
    oos_total_trades: int  # OOS 聚合交易数

    # 推荐的参数变更
    changes: List[ParamChange]

    # 推荐理由（自动生成）
    rationale: str

    # 审核/应用信息
    approved_at: Optional[datetime] = None
    applied_at: Optional[datetime] = None
    rolled_back_at: Optional[datetime] = None
    backup_path: Optional[str] = None  # 应用前的配置备份路径
    # 实验追踪 ID（跨 Research/Backtest/PaperTrading 关联）
    experiment_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可 JSON 化的字典。"""
        return {
            "rec_id": self.rec_id,
            "source_run_id": self.source_run_id,
            "created_at": self.created_at.isoformat(),
            "status": self.status.value,
            "overfitting_ratio": round(self.overfitting_ratio, 4),
            "consistency_rate": round(self.consistency_rate, 4),
            "oos_sharpe": round(self.oos_sharpe, 4),
            "oos_win_rate": round(self.oos_win_rate, 4),
            "oos_total_trades": self.oos_total_trades,
            "changes": [
                {
                    "section": c.section,
                    "key": c.key,
                    "old_value": c.old_value,
                    "new_value": round(c.new_value, 6),
                    "change_pct": round(c.change_pct, 2),
                }
                for c in self.changes
            ],
            "rationale": self.rationale,
            "approved_at": (self.approved_at.isoformat() if self.approved_at else None),
            "applied_at": (self.applied_at.isoformat() if self.applied_at else None),
            "rolled_back_at": (
                self.rolled_back_at.isoformat() if self.rolled_back_at else None
            ),
            "backup_path": self.backup_path,
            "experiment_id": self.experiment_id,
        }


def generate_run_id() -> str:
    """生成唯一的回测运行 ID。"""
    return f"bt_{uuid.uuid4().hex[:12]}"
