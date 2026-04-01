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


@dataclass(frozen=True)
class BacktestConfig:
    """回测运行配置。"""

    symbol: str
    timeframe: str
    start_time: datetime
    end_time: datetime
    strategies: Optional[List[str]] = None  # None = 全部策略
    initial_balance: float = 10000.0
    # 置信度管线开关
    enable_regime_affinity: bool = True
    enable_performance_tracker: bool = True
    enable_calibrator: bool = True
    enable_htf_alignment: bool = True
    # 最低开仓置信度
    min_confidence: float = 0.55
    # 最大同时持仓数
    max_positions: int = 3
    # 手续费（每手）
    commission_per_lot: float = 0.0
    # 滑点模拟（点数）
    slippage_points: float = 0.0
    # 参数覆盖（signal.ini [strategy_params] 格式）
    strategy_params: Dict[str, Any] = field(default_factory=dict)
    # Per-TF 参数覆盖（signal.ini [strategy_params.<TF>] 格式）
    strategy_params_per_tf: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    regime_affinity_overrides: Dict[str, Dict[str, float]] = field(default_factory=dict)
    # 策略-TF 白名单：{strategy_name: [tf1, tf2, ...]}，空 dict = 不限制
    strategy_timeframes: Dict[str, List[str]] = field(default_factory=dict)
    # per-strategy session 限制（与生产 signal.ini [strategy_sessions] 一致）
    # {strategy_name: [session1, session2, ...]}，空 dict = 不限制
    strategy_sessions: Dict[str, List[str]] = field(default_factory=dict)
    # 指标热身 bar 数量
    warmup_bars: int = 200
    # 合约大小（XAUUSD = 100 oz/lot）
    contract_size: float = 100.0
    # 风险百分比
    risk_percent: float = 1.0
    # 最小/最大下单手数（与实盘 sizing 约束一致）
    min_volume: float = 0.01
    max_volume: float = 1.0
    # 单笔/单日/单品种手数与频率限制
    max_volume_per_order: Optional[float] = None
    max_volume_per_symbol: Optional[float] = None
    max_volume_per_day: Optional[float] = None
    daily_loss_limit_pct: Optional[float] = None
    max_trades_per_day: Optional[int] = None
    max_trades_per_hour: Optional[int] = None
    # Regime 感知 SL/TP 缩放（与实盘 signal.ini [regime_sizing] 对齐）
    # trending: 给趋势更大的 TP 空间，SL 适度放宽避免被回调打掉
    regime_tp_trending: float = 1.30
    regime_sl_trending: float = 1.10
    # ranging: 快进快出，TP 和 SL 都收紧
    regime_tp_ranging: float = 0.75
    regime_sl_ranging: float = 0.85
    # breakout: 突破后波动大，TP 扩大抓波段
    regime_tp_breakout: float = 1.20
    regime_sl_breakout: float = 1.10
    # uncertain: 基准
    regime_tp_uncertain: float = 1.00
    regime_sl_uncertain: float = 1.00

    # ── Pending Entry 价格确认入场（复用实盘 pending_entry 纯逻辑）────────
    pending_entry_enabled: bool = False
    pending_entry_pullback_atr_factor: float = 0.3
    pending_entry_chase_atr_factor: float = 0.1
    pending_entry_momentum_atr_factor: float = 0.5
    pending_entry_symmetric_atr_factor: float = 0.4
    pending_entry_expiry_bars: int = 2  # Pending Entry 超时 bar 数

    # ── 持仓管理（复用实盘 position_rules）────────────────────────────────
    # breakeven / trailing stop（与实盘 PositionManager 相同参数）
    # 盈利 0.8 ATR 后开始 trailing（更快锁定利润）
    trailing_atr_multiplier: float = 0.8
    # 盈利 0.8 ATR 后移动 SL 到保本点
    breakeven_atr_threshold: float = 0.8
    # 日终自动平仓
    end_of_day_close_enabled: bool = False
    end_of_day_close_hour_utc: int = 21
    end_of_day_close_minute_utc: int = 0

    # ── 过滤器配置（模拟实盘 SignalFilterChain）──────────────────────────
    # 总开关：是否启用过滤器模拟
    filters_enabled: bool = True
    # 时段过滤
    filter_session_enabled: bool = True
    filter_allowed_sessions: str = "london,newyork"
    # 时段切换冷却（分钟）
    filter_session_transition_enabled: bool = True
    filter_session_transition_cooldown: int = 15
    # 波动率异常抑制（0 = 禁用）
    filter_volatility_enabled: bool = True
    filter_volatility_spike_multiplier: float = 2.5
    # 经济日历事件过滤（需要 DB 中有历史经济日历数据）
    filter_economic_enabled: bool = True
    filter_economic_lookahead_minutes: int = 30
    filter_economic_lookback_minutes: int = 15
    filter_economic_importance_min: int = 2
    # 点差过滤（回测中默认禁用，因为没有真实 spread 数据）
    filter_spread_enabled: bool = False
    filter_max_spread_points: float = 50.0

    # ── Trailing Take Profit（盈利后主动收缩 TP）────────────────────────
    trailing_tp_enabled: bool = False
    trailing_tp_activation_atr: float = 1.5  # 浮盈超过此 ATR 倍数后激活
    trailing_tp_trail_atr: float = 0.8       # 从最高盈利回撤的 ATR 距离

    # ── 指标驱动出场（持仓期间检测指标反转，收紧 SL）───────────────────
    indicator_exit_enabled: bool = False
    indicator_exit_supertrend_enabled: bool = True
    indicator_exit_supertrend_tighten_atr: float = 0.5
    indicator_exit_rsi_enabled: bool = True
    indicator_exit_rsi_overbought: float = 75.0
    indicator_exit_rsi_oversold: float = 25.0
    indicator_exit_rsi_delta_threshold: float = 5.0
    indicator_exit_rsi_tighten_atr: float = 0.5
    indicator_exit_macd_enabled: bool = True
    indicator_exit_macd_tighten_atr: float = 0.5
    indicator_exit_adx_enabled: bool = True
    indicator_exit_adx_entry_min: float = 25.0
    indicator_exit_adx_collapse_threshold: float = 10.0
    indicator_exit_adx_tighten_atr: float = 0.3

    # ── 连败熔断器（基于 bar 计数）──────────────────────────────────
    circuit_breaker_enabled: bool = False
    circuit_breaker_max_consecutive_losses: int = 5
    circuit_breaker_cooldown_bars: int = 20

    # ── 信号状态机回放（模拟实盘 preview→armed→confirmed 状态转换）────
    enable_state_machine: bool = False
    min_preview_stable_bars: int = 1  # preview 方向稳定 N 根 bar 后变为 armed

    # ── 信号评估记录上限（防止内存溢出）────────────────────────────
    max_signal_evaluations: int = 50000


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
    slippage_cost: float = 0.0  # 开仓+平仓滑点总成本
    commission_cost: float = 0.0  # 手续费总成本


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
    # 信号评估明细（用于回测质量分析）
    signal_evaluations: Optional[List[SignalEvaluation]] = None

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可 JSON 化的字典。"""
        from dataclasses import asdict

        result = asdict(self)
        # datetime 转字符串
        result["started_at"] = self.started_at.isoformat()
        result["completed_at"] = self.completed_at.isoformat()
        result["config"]["start_time"] = self.config.start_time.isoformat()
        result["config"]["end_time"] = self.config.end_time.isoformat()
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
class ParamChange:
    """单个参数变更。"""

    section: str  # "strategy_params" | "strategy_params.{TF}" | "regime_affinity.{strategy}"
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
        }


def generate_run_id() -> str:
    """生成唯一的回测运行 ID。"""
    return f"bt_{uuid.uuid4().hex[:12]}"
