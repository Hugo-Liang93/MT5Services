"""回测数据模型定义。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


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
    regime_affinity_overrides: Dict[str, Dict[str, float]] = field(
        default_factory=dict
    )
    # 指标热身 bar 数量
    warmup_bars: int = 200
    # 合约大小（XAUUSD = 100 oz/lot）
    contract_size: float = 100.0
    # 风险百分比
    risk_percent: float = 1.0

    # ── Pending Entry 价格确认入场（复用实盘 pending_entry 纯逻辑）────────
    enable_pending_entry: bool = False
    pending_entry_pullback_atr_factor: float = 0.3
    pending_entry_chase_atr_factor: float = 0.1
    pending_entry_momentum_atr_factor: float = 0.5
    pending_entry_symmetric_atr_factor: float = 0.4
    pending_entry_expiry_bars: int = 2  # Pending Entry 超时 bar 数

    # ── 持仓管理（复用实盘 position_rules）────────────────────────────────
    # breakeven / trailing stop（与实盘 PositionManager 相同参数）
    trailing_atr_multiplier: float = 1.0
    breakeven_atr_threshold: float = 1.0
    # 日终自动平仓
    end_of_day_close_enabled: bool = False
    end_of_day_close_hour_utc: int = 21
    end_of_day_close_minute_utc: int = 0

    # ── 过滤器配置（模拟实盘 SignalFilterChain）──────────────────────────
    # 总开关：是否启用过滤器模拟
    enable_filters: bool = True
    # 时段过滤
    filter_session_enabled: bool = True
    filter_allowed_sessions: str = "london,newyork"
    # 时段切换冷却（分钟）
    filter_session_transition_enabled: bool = True
    filter_session_transition_cooldown: int = 15
    # 波动率异常抑制（0 = 禁用）
    filter_volatility_enabled: bool = True
    filter_volatility_spike_multiplier: float = 2.5
    # 点差过滤（回测中默认禁用，因为没有真实 spread 数据）
    filter_spread_enabled: bool = False
    filter_max_spread_points: float = 50.0

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
    action: str  # "buy" | "sell"
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
    action: str  # "buy" | "sell" | "hold"
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
        result["equity_curve"] = [
            (t.isoformat(), v) for t, v in self.equity_curve
        ]
        for trade in result["trades"]:
            trade["entry_time"] = trade["entry_time"].isoformat() if isinstance(trade["entry_time"], datetime) else trade["entry_time"]
            trade["exit_time"] = trade["exit_time"].isoformat() if isinstance(trade["exit_time"], datetime) else trade["exit_time"]
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


def generate_run_id() -> str:
    """生成唯一的回测运行 ID。"""
    return f"bt_{uuid.uuid4().hex[:12]}"
