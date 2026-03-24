"""Paper Trading Bridge：将实盘信号流接入模拟组合，不经过 MT5 执行。

设计原则：
- 复用 PortfolioTracker 管理模拟持仓（与回测共享同一套 SL/TP/breakeven/trailing 逻辑）
- 作为 SignalRuntime 的 signal listener 接收实时信号
- 后台线程定期从 MarketDataService 获取报价，驱动持仓退出检查
- 线程安全：signal listener 和 price monitor 在不同线程运行，所有 portfolio 操作加锁
"""

from __future__ import annotations

import logging
import threading
import time as time_module
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.clients.mt5_market import OHLC
from src.market.service import MarketDataService
from src.signals.models import SignalEvent
from src.signals.service import SignalModule
from src.trading.sizing import compute_trade_params, extract_atr_from_indicators

from .metrics import compute_metrics
from .models import BacktestMetrics, TradeRecord
from .portfolio import PortfolioTracker

logger = logging.getLogger(__name__)


@dataclass
class PaperTradingConfig:
    """Paper Trading 配置。"""

    initial_balance: float = 10000.0
    contract_size: float = 100.0
    risk_percent: float = 1.0
    max_positions: int = 3
    commission_per_lot: float = 0.0
    slippage_points: float = 0.0
    trailing_atr_multiplier: float = 1.0
    breakeven_atr_threshold: float = 1.0
    min_confidence: float = 0.55
    end_of_day_close_enabled: bool = False
    end_of_day_close_hour_utc: int = 21


class PaperTradingBridge:
    """Paper Trading Bridge：实盘信号 -> 模拟执行。

    使用方式：
        bridge = PaperTradingBridge(config, market_service, signal_module)
        runtime.add_signal_listener(bridge.on_signal_event)
        bridge.start()

    信号流向：
        SignalRuntime -> on_signal_event() -> _execute_paper_trade()
                                                   |
                                           PortfolioTracker.open_position()

    价格监控：
        _price_monitor_loop() -> MarketDataService.get_quote()
                                         |
                                 PortfolioTracker.check_exits()
    """

    def __init__(
        self,
        config: PaperTradingConfig,
        market_service: MarketDataService,
        signal_module: SignalModule,
    ) -> None:
        self._config = config
        self._market_service = market_service
        self._signal_module = signal_module
        self._lock = threading.Lock()
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._bar_index = 0  # 单调递增的虚拟 bar 索引
        self._active_symbol: Optional[str] = None  # 最近交易的品种（用于价格监控）

        self._portfolio = self._create_portfolio()

        # 统计
        self._signals_received = 0
        self._signals_executed = 0
        self._signals_rejected = 0
        self._started_at: Optional[datetime] = None

    def _create_portfolio(self) -> PortfolioTracker:
        """根据配置创建 PortfolioTracker 实例。"""
        cfg = self._config
        return PortfolioTracker(
            initial_balance=cfg.initial_balance,
            max_positions=cfg.max_positions,
            commission_per_lot=cfg.commission_per_lot,
            slippage_points=cfg.slippage_points,
            contract_size=cfg.contract_size,
            trailing_atr_multiplier=cfg.trailing_atr_multiplier,
            breakeven_atr_threshold=cfg.breakeven_atr_threshold,
            end_of_day_close_enabled=cfg.end_of_day_close_enabled,
            end_of_day_close_hour_utc=cfg.end_of_day_close_hour_utc,
        )

    def on_signal_event(self, event: SignalEvent) -> None:
        """信号监听回调，由 SignalRuntime 在信号线程中调用。

        仅处理 confirmed_buy / confirmed_sell 事件，其他状态忽略。
        """
        self._signals_received += 1

        if event.signal_state not in ("confirmed_buy", "confirmed_sell"):
            return

        if event.confidence < self._config.min_confidence:
            self._signals_rejected += 1
            logger.debug(
                "paper_trade: 置信度不足，跳过 %s %s %.3f < %.3f",
                event.strategy,
                event.signal_state,
                event.confidence,
                self._config.min_confidence,
            )
            return

        self._execute_paper_trade(event)

    def start(self) -> None:
        """启动后台价格监控线程。"""
        if self._running:
            logger.warning("paper_trade: 已在运行中")
            return

        self._running = True
        self._started_at = datetime.now(timezone.utc)
        self._monitor_thread = threading.Thread(
            target=self._price_monitor_loop,
            name="paper-trade-monitor",
            daemon=True,
        )
        self._monitor_thread.start()
        logger.info(
            "paper_trade: 已启动，初始资金=%.2f，最大持仓=%d",
            self._config.initial_balance,
            self._config.max_positions,
        )

    def stop(self) -> None:
        """停止后台价格监控线程。"""
        if not self._running:
            return

        self._running = False
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=5.0)
            self._monitor_thread = None
        logger.info("paper_trade: 已停止")

    def status(self) -> Dict[str, Any]:
        """返回当前 Paper Trading 状态摘要。"""
        with self._lock:
            floating_pnl = self._portfolio._floating_pnl(
                self._get_last_price()
            )
            return {
                "running": self._running,
                "started_at": self._started_at.isoformat() if self._started_at else None,
                "initial_balance": self._config.initial_balance,
                "current_balance": round(self._portfolio.current_balance, 2),
                "floating_pnl": round(floating_pnl, 2),
                "equity": round(self._portfolio.current_balance + floating_pnl, 2),
                "open_positions": self._portfolio.open_position_count,
                "closed_trades": len(self._portfolio.closed_trades),
                "signals_received": self._signals_received,
                "signals_executed": self._signals_executed,
                "signals_rejected": self._signals_rejected,
            }

    def get_trades(self) -> List[TradeRecord]:
        """返回所有已关闭的交易记录。"""
        with self._lock:
            return self._portfolio.closed_trades

    def get_metrics(self) -> BacktestMetrics:
        """基于已关闭交易计算实时绩效指标。"""
        with self._lock:
            trades = self._portfolio.closed_trades
            equity_values = self._portfolio.equity_values
            return compute_metrics(
                trades=trades,
                initial_balance=self._config.initial_balance,
                equity_curve=equity_values if equity_values else [self._config.initial_balance],
            )

    def reset(self) -> None:
        """重置 Paper Trading 状态（需先 stop）。"""
        if self._running:
            self.stop()

        with self._lock:
            self._portfolio = self._create_portfolio()
            self._bar_index = 0
            self._signals_received = 0
            self._signals_executed = 0
            self._signals_rejected = 0
            self._started_at = None
        logger.info("paper_trade: 已重置")

    # ── 内部方法 ──────────────────────────────────────────────────────

    def _execute_paper_trade(self, event: SignalEvent) -> None:
        """将确认信号转化为模拟交易。

        从信号事件的 indicators 快照中提取 ATR，计算 SL/TP 和仓位大小，
        然后调用 PortfolioTracker.open_position()。
        """
        atr_value = extract_atr_from_indicators(event.indicators)
        if atr_value is None or atr_value <= 0:
            self._signals_rejected += 1
            logger.warning(
                "paper_trade: 无法提取 ATR，跳过 %s %s",
                event.strategy,
                event.signal_state,
            )
            return

        action = "buy" if event.signal_state == "confirmed_buy" else "sell"
        self._active_symbol = event.symbol

        # 获取当前报价作为入场价
        quote = self._market_service.get_quote(event.symbol)
        if quote is None:
            self._signals_rejected += 1
            logger.warning(
                "paper_trade: 无法获取报价 %s，跳过",
                event.symbol,
            )
            return

        mid_price = (quote.bid + quote.ask) / 2.0

        with self._lock:
            trade_params = compute_trade_params(
                action=action,
                current_price=mid_price,
                atr_value=atr_value,
                account_balance=self._portfolio.current_balance,
                timeframe=event.timeframe,
                risk_percent=self._config.risk_percent,
                contract_size=self._config.contract_size,
            )

            # 构造模拟 bar 用于 PortfolioTracker
            mock_bar = OHLC(
                time=datetime.now(timezone.utc),
                open=mid_price,
                high=quote.ask,
                low=quote.bid,
                close=mid_price,
                volume=0,
                symbol=event.symbol,
                timeframe="LIVE",
            )

            self._bar_index += 1
            regime = event.metadata.get("regime", "unknown")

            opened = self._portfolio.open_position(
                strategy=event.strategy,
                action=action,
                bar=mock_bar,
                trade_params=trade_params,
                regime=str(regime),
                confidence=event.confidence,
                bar_index=self._bar_index,
                atr_at_entry=atr_value,
            )

        if opened:
            self._signals_executed += 1
            logger.info(
                "paper_trade: 开仓 %s %s@%.2f SL=%.2f TP=%.2f size=%.2f conf=%.3f [%s]",
                action,
                event.symbol,
                mid_price,
                trade_params.stop_loss,
                trade_params.take_profit,
                trade_params.position_size,
                event.confidence,
                event.strategy,
            )
        else:
            self._signals_rejected += 1
            logger.debug(
                "paper_trade: 开仓被拒绝（持仓已满），%s %s",
                event.strategy,
                action,
            )

    def _price_monitor_loop(self) -> None:
        """后台线程：定期获取报价并检查持仓退出条件。

        每 1 秒轮询一次，从 MarketDataService 获取最新报价，
        构造模拟 bar 并调用 PortfolioTracker.check_exits()。
        """
        logger.info("paper_trade: 价格监控线程已启动")

        while self._running:
            try:
                self._check_all_positions()
            except Exception:
                logger.exception("paper_trade: 价格监控异常")

            time_module.sleep(1.0)

        logger.info("paper_trade: 价格监控线程已退出")

    def _check_all_positions(self) -> None:
        """检查所有持仓的退出条件。"""
        with self._lock:
            if self._portfolio.open_position_count == 0:
                return
            symbol = self._active_symbol

        if not symbol:
            return

        quote = self._market_service.get_quote(symbol)
        if quote is None:
            return

            bid = quote.bid
            ask = quote.ask
            mid_price = (bid + ask) / 2.0

            self._bar_index += 1

            mock_bar = OHLC(
                time=datetime.now(timezone.utc),
                open=mid_price,
                high=ask,
                low=bid,
                close=mid_price,
                volume=0,
                symbol=quote.symbol,
                timeframe="LIVE",
            )

            closed_trades = self._portfolio.check_exits(mock_bar, self._bar_index)

            for trade in closed_trades:
                logger.info(
                    "paper_trade: 平仓 %s %s PnL=%.2f 原因=%s [%s]",
                    trade.direction,
                    trade.strategy,
                    trade.pnl,
                    trade.exit_reason,
                    trade.strategy,
                )

    def _get_last_price(self) -> float:
        """获取最新中间价，用于浮动盈亏计算。"""
        symbol = self._active_symbol
        if symbol:
            quote = self._market_service.get_quote(symbol)
            if quote is not None:
                return (quote.bid + quote.ask) / 2.0
        return 0.0
