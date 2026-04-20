"""Paper Trading 数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class PaperTradeRecord:
    """单笔模拟交易记录。"""

    trade_id: str
    session_id: str
    strategy: str
    direction: str
    symbol: str
    timeframe: str
    entry_time: datetime
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: float
    confidence: float
    regime: str
    signal_id: str = ""
    # 平仓后填充
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    exit_reason: Optional[str] = None
    bars_held: int = 0
    slippage_cost: float = 0.0
    commission_cost: float = 0.0
    # 运行时状态
    current_sl: Optional[float] = None
    current_tp: Optional[float] = None
    breakeven_activated: bool = False
    trailing_activated: bool = False
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.exit_time is None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "session_id": self.session_id,
            "strategy": self.strategy,
            "direction": self.direction,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "entry_time": self.entry_time.isoformat(),
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "position_size": self.position_size,
            "confidence": self.confidence,
            "regime": self.regime,
            "signal_id": self.signal_id,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "exit_price": self.exit_price,
            "pnl": self.pnl,
            "pnl_pct": self.pnl_pct,
            "exit_reason": self.exit_reason,
            "bars_held": self.bars_held,
            "current_sl": self.current_sl,
            "current_tp": self.current_tp,
            "breakeven_activated": self.breakeven_activated,
            "trailing_activated": self.trailing_activated,
            "max_favorable_excursion": self.max_favorable_excursion,
            "max_adverse_excursion": self.max_adverse_excursion,
        }


@dataclass
class PaperSession:
    """Paper Trading Session 记录。"""

    session_id: str
    started_at: datetime
    initial_balance: float
    config_snapshot: Dict[str, Any] = field(default_factory=dict)
    stopped_at: Optional[datetime] = None
    # 实验追踪 ID（跨 Research/Backtest/PaperTrading 关联）
    experiment_id: Optional[str] = None
    # 关联的回测 run_id（从 Recommendation 启动时填充）
    source_backtest_run_id: Optional[str] = None
    # 关联的 recommendation_id（P10.5：paper session ↔ recommendation 反查链路）
    recommendation_id: Optional[str] = None
    final_balance: Optional[float] = None
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: Optional[float] = None

    @property
    def is_active(self) -> bool:
        return self.stopped_at is None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "experiment_id": self.experiment_id,
            "source_backtest_run_id": self.source_backtest_run_id,
            "recommendation_id": self.recommendation_id,
            "started_at": self.started_at.isoformat(),
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
            "initial_balance": self.initial_balance,
            "final_balance": self.final_balance,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "total_pnl": round(self.total_pnl, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "sharpe_ratio": (
                round(self.sharpe_ratio, 4) if self.sharpe_ratio is not None else None
            ),
            "is_active": self.is_active,
        }


@dataclass
class PaperMetrics:
    """Paper Trading 实时绩效指标。"""

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    max_drawdown_pct: float = 0.0
    profit_factor: float = 0.0
    avg_bars_held: float = 0.0
    best_trade_pnl: float = 0.0
    worst_trade_pnl: float = 0.0
    # 按策略/regime 细分
    by_strategy: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    by_regime: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": round(self.win_rate, 4),
            "total_pnl": round(self.total_pnl, 2),
            "avg_pnl": round(self.avg_pnl, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "profit_factor": round(self.profit_factor, 4),
            "avg_bars_held": round(self.avg_bars_held, 1),
            "best_trade_pnl": round(self.best_trade_pnl, 2),
            "worst_trade_pnl": round(self.worst_trade_pnl, 2),
            "by_strategy": self.by_strategy,
            "by_regime": self.by_regime,
        }
