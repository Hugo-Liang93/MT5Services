"""PaperTradingBridge：将实盘信号流接入模拟组合。

设计原则：
- 作为 SignalRuntime 的 signal listener 接收确认信号
- 后台线程定期从 MarketDataService 获取报价，驱动持仓退出检查
- 线程安全：所有 portfolio 操作加锁
- 不经过 MT5 执行，不影响实盘任何数据
- 实现 RuntimeManagedComponent Protocol
"""

from __future__ import annotations

import logging
import threading
import time as time_mod
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from src.signals.metadata_keys import MetadataKey as MK
from src.signals.models import SignalEvent
from src.trading.execution.sizing import extract_atr_from_indicators
from src.utils.timezone import utc_now

from .config import PaperTradingConfig
from .models import PaperMetrics, PaperSession, PaperTradeRecord
from .portfolio import PaperPortfolio

logger = logging.getLogger(__name__)


class PaperTradingBridge:
    """Paper Trading Bridge：实盘信号 → 模拟执行。

    生命周期：
        bridge = PaperTradingBridge(config, market_quote_fn)
        signal_runtime.add_signal_listener(bridge.on_signal_event)
        bridge.start()
        ...
        bridge.stop()

    信号流向：
        SignalRuntime → on_signal_event() → portfolio.open_position()

    价格监控：
        _price_monitor_loop() → market_quote_fn() → portfolio.check_exits()
    """

    # RuntimeManagedComponent 属性
    name = "paper_trading"
    supported_modes = frozenset({"full", "observe"})

    def __init__(
        self,
        config: PaperTradingConfig,
        market_quote_fn: Callable[[str], Any],
        *,
        on_trade_closed: Optional[Callable[[PaperTradeRecord], None]] = None,
        on_trade_opened: Optional[Callable[[PaperTradeRecord], None]] = None,
    ) -> None:
        self._config = config
        self._market_quote_fn = market_quote_fn
        self._on_trade_closed = on_trade_closed
        self._on_trade_opened = on_trade_opened
        self._lock = threading.Lock()
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None

        # Session（公开只读访问供装配层 stop 流程使用）
        self._session: Optional[PaperSession] = None
        self._portfolio: Optional[PaperPortfolio] = None
        self._active_symbols: set[str] = set()

        # 实验追踪
        self._experiment_id: Optional[str] = None
        self._source_backtest_run_id: Optional[str] = None

        # 统计
        self._signals_received = 0
        self._signals_executed = 0
        self._signals_rejected = 0
        self._reject_reasons: Dict[str, int] = {}

    # ── RuntimeManagedComponent Protocol ───────────────────────────

    def set_experiment_context(
        self,
        experiment_id: Optional[str] = None,
        source_backtest_run_id: Optional[str] = None,
    ) -> None:
        """设置实验追踪上下文（需在 start() 之前调用）。"""
        self._experiment_id = experiment_id
        self._source_backtest_run_id = source_backtest_run_id

    @property
    def session(self) -> Optional["PaperSession"]:
        """当前 session（只读，供装配层 stop 流程访问）。"""
        return self._session

    def start(self) -> None:
        """启动 Paper Trading session 和价格监控线程。"""
        if self._running:
            logger.warning("paper_trading: 已在运行中")
            return

        if not self._config.enabled:
            logger.info("paper_trading: 未启用 (enabled=false)")
            return

        session_id = f"ps_{uuid4().hex[:12]}"
        self._session = PaperSession(
            session_id=session_id,
            started_at=utc_now(),
            initial_balance=self._config.initial_balance,
            config_snapshot=self._config.to_dict(),
            experiment_id=self._experiment_id,
            source_backtest_run_id=self._source_backtest_run_id,
        )
        self._portfolio = PaperPortfolio(self._config, session_id)
        self._active_symbols.clear()
        self._signals_received = 0
        self._signals_executed = 0
        self._signals_rejected = 0
        self._reject_reasons.clear()

        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._price_monitor_loop,
            name="paper-trade-monitor",
            daemon=True,
        )
        self._monitor_thread.start()
        logger.info(
            "paper_trading: session %s 已启动，初始资金=%.2f，最大持仓=%d",
            session_id,
            self._config.initial_balance,
            self._config.max_positions,
        )

    def stop(self) -> None:
        """停止 Paper Trading，平仓所有持仓，关闭 session。"""
        if not self._running:
            return

        self._running = False
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=5.0)
            self._monitor_thread = None

        with self._lock:
            if self._portfolio is not None and self._session is not None:
                # 尝试获取报价平仓
                bid, ask = self._get_best_quote()
                if bid > 0 and ask > 0:
                    closed = self._portfolio.force_close_all(bid, ask)
                    for trade in closed:
                        logger.info(
                            "paper_trading: session 结束平仓 %s %s PnL=%.2f",
                            trade.direction,
                            trade.strategy,
                            trade.pnl or 0,
                        )
                        if self._on_trade_closed:
                            self._on_trade_closed(trade)

                # 填充 session 结束信息
                metrics = self._portfolio.compute_metrics()
                self._session.stopped_at = utc_now()
                self._session.final_balance = self._portfolio.current_balance
                self._session.total_trades = metrics.total_trades
                self._session.winning_trades = metrics.winning_trades
                self._session.losing_trades = metrics.losing_trades
                self._session.total_pnl = metrics.total_pnl
                self._session.max_drawdown_pct = metrics.max_drawdown_pct

        logger.info("paper_trading: session 已停止")

    def is_running(self) -> bool:
        return self._running

    def snapshot(self) -> Dict[str, Any]:
        return self.status()

    # ── Signal Listener ────────────────────────────────────────────

    def on_signal_event(self, event: SignalEvent) -> None:
        """信号监听回调。仅处理 confirmed_buy / confirmed_sell。"""
        if not self._running or self._portfolio is None:
            return

        self._signals_received += 1

        if event.signal_state not in ("confirmed_buy", "confirmed_sell"):
            return

        if event.confidence < self._config.min_confidence:
            self._signals_rejected += 1
            self._reject_reasons["low_confidence"] = (
                self._reject_reasons.get("low_confidence", 0) + 1
            )
            return

        atr_value = extract_atr_from_indicators(event.indicators)
        if atr_value is None or atr_value <= 0:
            self._signals_rejected += 1
            self._reject_reasons["no_atr"] = self._reject_reasons.get("no_atr", 0) + 1
            return

        direction = "buy" if event.signal_state == "confirmed_buy" else "sell"

        # 获取当前报价
        quote = self._market_quote_fn(event.symbol)
        if quote is None:
            self._signals_rejected += 1
            self._reject_reasons["no_quote"] = (
                self._reject_reasons.get("no_quote", 0) + 1
            )
            return

        mid_price = (quote.bid + quote.ask) / 2.0
        regime = str(
            event.metadata.get(MK.REGIME) or event.metadata.get(MK.REGIME_HARD) or "unknown"
        )

        with self._lock:
            record = self._portfolio.open_position(
                strategy=event.strategy,
                direction=direction,
                symbol=event.symbol,
                timeframe=event.timeframe,
                entry_price=mid_price,
                atr_value=atr_value,
                confidence=event.confidence,
                regime=regime,
                signal_id=event.signal_id,
                indicators=event.indicators,
            )

        if record is not None:
            self._signals_executed += 1
            self._active_symbols.add(event.symbol)
            logger.info(
                "paper_trade: 开仓 %s %s@%.2f SL=%.2f TP=%.2f size=%.2f "
                "conf=%.3f [%s] regime=%s",
                direction,
                event.symbol,
                record.entry_price,
                record.stop_loss,
                record.take_profit,
                record.position_size,
                event.confidence,
                event.strategy,
                regime,
            )
            if self._on_trade_opened:
                self._on_trade_opened(record)
        else:
            self._signals_rejected += 1
            self._reject_reasons["max_positions"] = (
                self._reject_reasons.get("max_positions", 0) + 1
            )

    # ── 查询接口 ───────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        """返回当前 Paper Trading 状态摘要。"""
        with self._lock:
            if self._portfolio is None or self._session is None:
                return {
                    "running": self._running,
                    "session": None,
                    "signals_received": self._signals_received,
                }

            bid, ask = self._get_best_quote()
            floating = self._portfolio.floating_pnl(bid, ask) if bid > 0 else 0.0
            equity = (
                self._portfolio.equity(bid, ask)
                if bid > 0
                else self._portfolio.current_balance
            )

            return {
                "running": self._running,
                "session_id": self._session.session_id,
                "started_at": self._session.started_at.isoformat(),
                "initial_balance": self._config.initial_balance,
                "current_balance": round(self._portfolio.current_balance, 2),
                "floating_pnl": round(floating, 2),
                "equity": round(equity, 2),
                "open_positions": self._portfolio.open_position_count,
                "closed_trades": len(self._portfolio.closed_trades),
                "signals_received": self._signals_received,
                "signals_executed": self._signals_executed,
                "signals_rejected": self._signals_rejected,
                "reject_reasons": dict(self._reject_reasons),
                "active_symbols": sorted(self._active_symbols),
            }

    def get_closed_trades(self) -> List[Dict[str, Any]]:
        with self._lock:
            if self._portfolio is None:
                return []
            return [t.to_dict() for t in self._portfolio.closed_trades]

    def get_open_positions(self) -> List[Dict[str, Any]]:
        with self._lock:
            if self._portfolio is None:
                return []
            return [t.to_dict() for t in self._portfolio._open_positions.values()]

    def get_metrics(self) -> Dict[str, Any]:
        with self._lock:
            if self._portfolio is None:
                return PaperMetrics().to_dict()
            return self._portfolio.compute_metrics().to_dict()

    def get_session(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self._session is None:
                return None
            return self._session.to_dict()

    def reset(self) -> None:
        """重置 Paper Trading（需先 stop）。"""
        if self._running:
            self.stop()
        with self._lock:
            self._session = None
            self._portfolio = None
            self._active_symbols.clear()
            self._signals_received = 0
            self._signals_executed = 0
            self._signals_rejected = 0
            self._reject_reasons.clear()
        logger.info("paper_trading: 已重置")

    # ── 内部方法 ──────────────────────────────────────────────────────

    def _price_monitor_loop(self) -> None:
        """后台线程：定期获取报价并检查退出条件。"""
        logger.info("paper_trading: 价格监控线程已启动")
        snapshot_counter = 0

        while self._running:
            try:
                self._check_all_positions()
                snapshot_counter += 1
                # 每 30 次轮询记录一次权益快照
                if snapshot_counter >= 30:
                    snapshot_counter = 0
                    self._snapshot_equity()
            except Exception:
                logger.exception("paper_trading: 价格监控异常")

            time_mod.sleep(self._config.price_monitor_interval)

        logger.info("paper_trading: 价格监控线程已退出")

    def _check_all_positions(self) -> None:
        """检查所有持仓的退出条件。"""
        with self._lock:
            if self._portfolio is None or self._portfolio.open_position_count == 0:
                return

            bid, ask = self._get_best_quote()
            if bid <= 0 or ask <= 0:
                return

            closed = self._portfolio.check_exits(bid, ask)

        for trade in closed:
            logger.info(
                "paper_trade: 平仓 %s %s PnL=%.2f 原因=%s [%s]",
                trade.direction,
                trade.symbol,
                trade.pnl or 0,
                trade.exit_reason,
                trade.strategy,
            )
            if self._on_trade_closed:
                self._on_trade_closed(trade)

    def _snapshot_equity(self) -> None:
        """记录权益快照。"""
        with self._lock:
            if self._portfolio is None:
                return
            bid, ask = self._get_best_quote()
            if bid > 0 and ask > 0:
                self._portfolio.snapshot_equity(bid, ask)

    def _get_best_quote(self) -> tuple[float, float]:
        """获取活跃品种的报价。返回 (bid, ask)，无报价返回 (0, 0)。"""
        for symbol in self._active_symbols:
            quote = self._market_quote_fn(symbol)
            if quote is not None:
                return float(quote.bid), float(quote.ask)
        return 0.0, 0.0
