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
        return result


@dataclass
class ParameterSpace:
    """参数搜索空间定义。"""

    strategy_params: Dict[str, List[Any]] = field(default_factory=dict)
    regime_affinity_overrides: Dict[str, Dict[str, List[float]]] = field(
        default_factory=dict
    )
    search_mode: str = "grid"  # "grid" | "random"
    max_combinations: int = 500  # random 模式下的最大采样数


def generate_run_id() -> str:
    """生成唯一的回测运行 ID。"""
    return f"bt_{uuid.uuid4().hex[:12]}"
