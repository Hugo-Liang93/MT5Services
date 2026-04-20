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
        recover_fn: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
        admission_writer: Optional[Callable[[str, str, Optional[str]], None]] = None,
    ) -> None:
        self._config = config
        self._market_quote_fn = market_quote_fn
        self._on_trade_closed = on_trade_closed
        self._on_trade_opened = on_trade_opened
        # recover_fn: 可选 — 由 builder 注入，返回 {session, open_trades} dict 让 start() 复用。
        # 如果 config.resume_active_session=True 且该函数返回非 None，则 start() 恢复老 session。
        self._recover_fn = recover_fn
        # P9 bug #3: paper bridge 直接消费 signal 不走 ExecutionIntentPublisher 链路，
        # admission writeback listener 收不到 paper signal。在此回调中回填 signal_events
        # 的 actionability/guard_reason_code 字段，让 paper signal 也参与 priority 排序。
        # 签名: (signal_id, actionability, guard_reason_code) -> None
        self._admission_writer = admission_writer
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

        # 重置本进程统计（resume 情况下也应清零：这些是当前进程的计数，跨进程不合并）。
        # _active_symbols 同理先清空；resume 路径内部会按 open trades 重新填充。
        self._active_symbols.clear()
        self._signals_received = 0
        self._signals_executed = 0
        self._signals_rejected = 0
        self._reject_reasons.clear()

        # 进程重启 recovery：如果上次未干净 stop 且 config.resume_active_session=True，
        # 尝试复用之前的 session_id + open trades，保持 Paper 状态连续。
        resumed = self._attempt_resume()

        if not resumed:
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

        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._price_monitor_loop,
            name="paper-trade-monitor",
            daemon=True,
        )
        self._monitor_thread.start()
        # resume 路径下 session 已在 _attempt_resume 里设置；fresh start 路径刚创建
        active_session_id = self._session.session_id if self._session else ""
        logger.info(
            "paper_trading: session %s 已%s，初始资金=%.2f，最大持仓=%d",
            active_session_id,
            "恢复" if resumed else "启动",
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
            # P9 bug #3: hold/preview 信号也回填 admission（hold actionability）
            self._writeback_admission(event.signal_id, "hold", "non_confirmed_state")
            return

        if event.confidence < self._config.min_confidence:
            self._signals_rejected += 1
            self._reject_reasons["low_confidence"] = (
                self._reject_reasons.get("low_confidence", 0) + 1
            )
            self._writeback_admission(event.signal_id, "blocked", "min_confidence")
            return

        atr_value = extract_atr_from_indicators(event.indicators)
        if atr_value is None or atr_value <= 0:
            self._signals_rejected += 1
            self._reject_reasons["no_atr"] = self._reject_reasons.get("no_atr", 0) + 1
            self._writeback_admission(
                event.signal_id, "blocked", "trade_params_unavailable"
            )
            return

        direction = "buy" if event.signal_state == "confirmed_buy" else "sell"

        # 获取当前报价
        quote = self._market_quote_fn(event.symbol)
        if quote is None:
            self._signals_rejected += 1
            self._reject_reasons["no_quote"] = (
                self._reject_reasons.get("no_quote", 0) + 1
            )
            self._writeback_admission(event.signal_id, "blocked", "quote_stale")
            return

        mid_price = (quote.bid + quote.ask) / 2.0
        regime = str(
            event.metadata.get(MK.REGIME)
            or event.metadata.get(MK.REGIME_HARD)
            or "unknown"
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
            self._writeback_admission(event.signal_id, "actionable", None)
        else:
            self._signals_rejected += 1
            self._reject_reasons["max_positions"] = (
                self._reject_reasons.get("max_positions", 0) + 1
            )
            self._writeback_admission(
                event.signal_id, "blocked", "max_concurrent_positions_per_symbol"
            )

    def _writeback_admission(
        self,
        signal_id: Optional[str],
        actionability: str,
        guard_reason_code: Optional[str],
    ) -> None:
        """P9 bug #3: paper signal admission 回填到 signal_events 表。

        签名匹配 admission_writer 协议；缺失 writer 时静默跳过。
        异常不向上抛 — paper bridge 不该因 DB 写失败而中断信号处理。
        """
        if self._admission_writer is None or not signal_id:
            return
        try:
            self._admission_writer(signal_id, actionability, guard_reason_code)
        except Exception:
            logger.debug(
                "paper admission writeback failed for signal_id=%s",
                signal_id,
                exc_info=True,
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

    def snapshot_active_session(self) -> Optional[PaperSession]:
        """返回当前 session 的实时快照（包含最新 metrics）。

        供 PaperTradeTracker 在 flush 时主动拉取，用于持久化运行中的 session 元数据。
        与 stop() 末尾设置 final_balance 的逻辑等价，但允许在不停止 session 的前提下
        把当前累计 PnL / trades 计数写入 DB。
        """
        with self._lock:
            if self._session is None or self._portfolio is None:
                return None
            metrics = self._portfolio.compute_metrics()
            session = self._session
            # 注意：不修改 stopped_at / final_balance（保留 None 表示仍 active）
            session.total_trades = metrics.total_trades
            session.winning_trades = metrics.winning_trades
            session.losing_trades = metrics.losing_trades
            session.total_pnl = metrics.total_pnl
            session.max_drawdown_pct = metrics.max_drawdown_pct
            session.sharpe_ratio = (
                metrics.profit_factor if metrics.profit_factor > 0 else None
            )
            return session

    def _attempt_resume(self) -> bool:
        """进程重启 recovery：尝试复用上次未完结的 session + open trades。

        返回 True 表示已恢复（self._session / self._portfolio 已被 set），
        False 表示 fresh start。

        仅在 config.resume_active_session=True 且 recover_fn 返回有效 payload 时生效。
        恢复语义：
          - session_id 复用，started_at 保留
          - balance = initial_balance + total_pnl（closed trades 的 realized pnl 近似基线）
          - open trades 通过 portfolio.restore_open_trade() 放回 _open_positions
          - 不恢复 closed_trades 列表（保持简单；历史 trades 已在 DB 可查）
        """
        if not getattr(self._config, "resume_active_session", False):
            return False
        if self._recover_fn is None:
            return False
        try:
            payload = self._recover_fn()
        except Exception:
            logger.warning(
                "paper_trading: recover_fn 失败，走 fresh start", exc_info=True
            )
            return False
        if not payload:
            return False
        session_row = payload.get("session")
        open_trade_records = payload.get("open_trades") or []
        if not session_row:
            return False

        from datetime import datetime as _dt

        session_id = str(session_row["session_id"])
        started_at_raw = session_row.get("started_at") or utc_now()
        if isinstance(started_at_raw, str):
            try:
                started_at = _dt.fromisoformat(started_at_raw)
            except Exception:
                started_at = utc_now()
        else:
            started_at = started_at_raw
        initial_balance = float(
            session_row.get("initial_balance") or self._config.initial_balance
        )
        total_pnl = float(session_row.get("total_pnl") or 0.0)

        self._session = PaperSession(
            session_id=session_id,
            started_at=started_at,
            initial_balance=initial_balance,
            config_snapshot=self._config.to_dict(),
            experiment_id=self._experiment_id,
            source_backtest_run_id=self._source_backtest_run_id,
            total_pnl=total_pnl,
            total_trades=int(session_row.get("total_trades") or 0),
            winning_trades=int(session_row.get("winning_trades") or 0),
            losing_trades=int(session_row.get("losing_trades") or 0),
            max_drawdown_pct=float(session_row.get("max_drawdown_pct") or 0.0),
        )
        self._portfolio = PaperPortfolio(self._config, session_id)
        # balance 近似 = initial + realized pnl（closed trades 净利润）；
        # floating pnl 会在价格监控下次循环重新计算
        self._portfolio.restore_baseline(balance=initial_balance + total_pnl)
        for record in open_trade_records:
            self._portfolio.restore_open_trade(record)
            if record.symbol:
                self._active_symbols.add(record.symbol)
        logger.info(
            "paper_trading: session %s 已恢复（open_trades=%d, balance≈%.2f）",
            session_id,
            len(open_trade_records),
            initial_balance + total_pnl,
        )
        return True

    def snapshot_open_trades(self) -> List[PaperTradeRecord]:
        """返回当前 open positions 的 PaperTradeRecord 副本列表。

        供 tracker 在 flush 时主动拉取，周期性 upsert 到 DB，
        让 open trade 的 current_sl / current_tp / MFE / MAE / bars_held 等运行时字段
        随时间推进同步到 paper_trade_outcomes（配合 INSERT_TRADE_SQL 的 ON CONFLICT DO UPDATE）。
        返回副本而非原始对象，避免在锁外被 portfolio 线程并发修改字段。
        """
        with self._lock:
            if self._portfolio is None:
                return []
            # shallow copy via dataclasses.replace 或直接构造——PaperTradeRecord 是 dataclass
            from dataclasses import replace as _dc_replace

            return [_dc_replace(t) for t in self._portfolio._open_positions.values()]

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
