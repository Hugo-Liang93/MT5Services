"""TradeExecutor：消费确认信号并执行自动下单。

职责边界保持清晰：
- 信号模块只负责生成并发布 `SignalEvent`
- `TradeExecutor` 通过 `SignalRuntime.add_signal_listener()` 订阅事件并执行交易

在 `deps.py` 中的典型接入方式：
    executor = TradeExecutor(trading_module=_c.trade_module, config=cfg)
    signal_runtime.add_signal_listener(executor.on_signal_event)
"""

from __future__ import annotations

from collections import deque
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from src.signals.evaluation.performance import StrategyPerformanceTracker

from .sizing import (
    RegimeSizing,
    extract_atr_from_indicators,
)
from .eventing import (
    emit_execution_blocked as _emit_execution_blocked_helper,
    emit_execution_decided as _emit_execution_decided_helper,
    execute_market_order as _execute_market_order_helper,
    notify_skip as _notify_skip_helper,
)
from src.monitoring.pipeline import PipelineEventBus
from src.signals.models import SignalEvent
from .gate import ExecutionGate, ExecutionGateConfig
from .params import (
    compute_params as _compute_params_helper,
    estimate_cost_metrics as _estimate_cost_metrics_helper,
    estimate_price as _estimate_price_helper,
    get_account_balance as _get_account_balance_helper,
    tf_to_seconds as _tf_to_seconds_helper,
)
from .pending_orders import (
    duplicate_execution_reason as _duplicate_execution_reason_helper,
    reached_position_limit as _reached_position_limit_helper,
    submit_pending_entry as _submit_pending_entry_helper,
)
from ..pending.manager import PendingEntryManager
from ..ports import ExecutorTradingPort
from ..positions.manager import PositionManager
from ..tracking.trade_outcome import TradeOutcomeTracker

logger = logging.getLogger(__name__)


@dataclass
class ExecutorConfig:
    enabled: bool = False
    min_confidence: float = 0.7
    # 按时间框架覆盖最低交易置信度，低周期可以设置更严格阈值。
    timeframe_min_confidence: dict[str, float] = field(default_factory=dict)
    # 在这些低周期上，一旦 HTF 方向冲突就直接阻止开仓。
    htf_conflict_block_timeframes: frozenset[str] = field(default_factory=frozenset)
    # 豁免 HTF 冲突拦截的策略类别，例如均值回归天然允许逆势。
    htf_conflict_exempt_categories: frozenset[str] = field(
        default_factory=lambda: frozenset({"reversion"})
    )
    max_concurrent_positions_per_symbol: int | None = 3
    risk_percent: float = 1.0
    sl_atr_multiplier: float = 1.5
    tp_atr_multiplier: float = 3.0
    min_volume: float = 0.01
    max_volume: float = 1.0
    # 合约大小映射，未命中的品种回退到 `default`。
    contract_size_map: dict[str, float] = field(
        default_factory=lambda: {"XAUUSD": 100.0, "default": 100.0}
    )
    # 按时间框架覆盖仓位风险倍率，优先级高于 `sizing.py` 默认值。
    timeframe_risk_multipliers: dict[str, float] = field(default_factory=dict)
    default_volume: float = 0.01
    # 技术故障熔断阈值，连续失败达到上限后暂停自动交易。
    max_consecutive_failures: int = 3
    # 熔断器自动恢复时间，单位分钟。
    circuit_auto_reset_minutes: int = 30
    max_spread_to_stop_ratio: float = 0.33
    # 同策略同方向再入场冷却 bar 数（0=禁止同向再入场，N=间隔 N 根 bar 后允许加仓）
    reentry_cooldown_bars: int = 3
    regime_sizing: RegimeSizing = field(default_factory=RegimeSizing)


class TradeExecutor:
    """Subscribes to SignalRuntime events and auto-executes confirmed trades.

    Execution is **non-blocking**: ``on_signal_event()`` enqueues the event
    and returns immediately so that SignalRuntime's main loop is never stalled
    by slow MT5 API calls.  A background daemon thread drains the queue.
    """

    def __init__(
        self,
        trading_module: ExecutorTradingPort,
        config: ExecutorConfig | None = None,
        account_balance_getter: Any | None = None,
        position_manager: PositionManager | None = None,
        persist_execution_fn: Callable[[list], None] | None = None,
        trade_outcome_tracker: TradeOutcomeTracker | None = None,
        on_execution_skip: Callable[[str, str], None] | None = None,
        execution_gate: ExecutionGate | None = None,
        pending_entry_manager: PendingEntryManager | None = None,
        performance_tracker: "StrategyPerformanceTracker | None" = None,
        pipeline_event_bus: PipelineEventBus | None = None,
    ):
        self._trading = trading_module
        self.config = config or ExecutorConfig()
        self._account_balance_getter = account_balance_getter
        self._position_manager = position_manager
        # 执行记录持久化回调，通常落到 `auto_executions` 一类存储。
        self._persist_execution_fn = persist_execution_fn
        self._trade_outcome_tracker = trade_outcome_tracker
        # 跳过执行时的通知回调，向下游传递 `(signal_id, reason)`。
        self._on_execution_skip = on_execution_skip
        # 成交监听回调，向外暴露标准化执行日志 `log_entry`。
        self._on_trade_executed: list[Callable[[dict], None]] = []
        # 前置准入过滤器，处理 voting group、armed 等执行门规则。
        self._execution_gate = execution_gate or ExecutionGate()
        # 价格确认入场管理器；为空时直接市价执行。
        self._pending_manager = pending_entry_manager
        # 日内绩效跟踪器，用于 PnL 熔断。
        self._performance_tracker = performance_tracker
        self._pipeline_event_bus = pipeline_event_bus
        self._execution_count = 0
        self._last_execution_at: datetime | None = None
        self._last_error: str | None = None
        self._last_risk_block: str | None = None
        self._execution_log: deque[dict] = deque(maxlen=100)
        # Async execution: decouple listener callback from MT5 API calls
        exec_queue_size = int(getattr(config, "exec_queue_size", 0) or 0) or 256
        self._exec_queue: queue.Queue = queue.Queue(maxsize=exec_queue_size)
        self._exec_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # 统计收到的 confirmed 事件总数。
        self._signals_received: int = 0
        self._signals_passed: int = 0
        self._skip_reasons: dict[str, int] = {}
        self._skip_lock = threading.Lock()
        # 分时间框架统计接收/放行/拦截原因。
        self._tf_stats: dict[str, dict[str, Any]] = {}
        self._margin_guard: Any = None  # Optional[MarginGuard], injected via set_margin_guard()
        self._execution_quality = {
            "recovered_from_state": 0,
            "risk_blocks": 0,
            "slippage_samples": 0,
            "slippage_total_price": 0.0,
            "slippage_total_points": 0.0,
            "queue_overflows": 0,
        }
        # 最近被丢弃的信号样本，供 `status()` 诊断队列溢出。
        self._dropped_signals: list[dict[str, Any]] = []
        self._max_dropped_history: int = 50
        # 连续失败计数与熔断状态。
        self._consecutive_failures: int = 0
        self._circuit_open: bool = False
        # 熔断打开时间，用于自动恢复计算。
        self._circuit_open_at: datetime | None = None
        # 同策略同方向再入场冷却：记录上次开仓的 bar_time
        # key = (symbol, strategy, direction), value = bar_time(datetime)
        self._last_entry_bar_time: dict[tuple[str, str, str], datetime] = {}

    # ------------------------------------------------------------------
    # Public listener interface
    # ------------------------------------------------------------------

    def on_signal_event(self, event: SignalEvent) -> None:
        """Called by SignalRuntime for every signal state transition.

        Non-blocking: enqueues the event for the background worker thread.
        Only acts on scope=confirmed, state-changing transitions with a buy/sell action.
        """
        if event.scope != "confirmed":
            return
        if "confirmed" not in event.signal_state:
            return
        # 没有 signal_id 的 confirmed 事件不进入执行链。
        if not event.signal_id:
            return
        # Ensure worker thread is running (lazy start)
        if self._exec_thread is None or not self._exec_thread.is_alive():
            self._start_worker()
        try:
            self._exec_queue.put_nowait(event)
        except queue.Full:
            # Backpressure retry for confirmed signals (extended to 3s)
            logger.warning(
                "TradeExecutor queue full, backpressure retry for %s/%s/%s",
                event.symbol, event.timeframe, event.strategy,
            )
            try:
                self._exec_queue.put(event, timeout=3.0)
            except queue.Full:
                self._execution_quality["queue_overflows"] += 1
                sig_id = getattr(event, "signal_id", "") or ""
                logger.error(
                    "TradeExecutor queue full after 3s retry, DROPPING confirmed event "
                    "%s/%s/%s signal_id=%s (overflows=%d). "
                    "Trading opportunity permanently lost!",
                    event.symbol, event.timeframe, event.strategy,
                    sig_id,
                    self._execution_quality["queue_overflows"],
                )
                # 保留最近一批被丢弃的 confirmed 信号，便于通过 status() 排查。
                self._dropped_signals.append({
                    "signal_id": sig_id,
                    "symbol": event.symbol,
                    "timeframe": event.timeframe,
                    "strategy": event.strategy,
                    "direction": getattr(event, "direction", ""),
                    "dropped_at": time.time(),
                })
                if len(self._dropped_signals) > self._max_dropped_history:
                    self._dropped_signals = self._dropped_signals[-self._max_dropped_history:]

    def _start_worker(self) -> None:
        """启动后台执行线程。"""
        self._stop_event.clear()
        t = threading.Thread(target=self._exec_worker, name="trade-executor", daemon=True)
        t.start()
        self._exec_thread = t

    def start(self) -> None:
        """启用执行器，允许后续 confirmed 事件重新拉起执行线程。"""
        self._stop_event.clear()

    def is_running(self) -> bool:
        return self._exec_thread is not None and self._exec_thread.is_alive()

    def _exec_worker(self) -> None:
        """后台消费执行队列，串行处理 confirmed 交易事件。"""
        while not self._stop_event.is_set() or not self._exec_queue.empty():
            try:
                event = self._exec_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._handle_confirmed(event)
            except Exception:
                logger.error("TradeExecutor worker error", exc_info=True)
            finally:
                self._exec_queue.task_done()

    def flush(self, timeout: float = 5.0) -> None:
        """等待执行队列清空，超时则抛出 TimeoutError。"""
        deadline = time.monotonic() + max(0.0, timeout)
        while self._exec_queue.unfinished_tasks > 0:
            if time.monotonic() >= deadline:
                raise TimeoutError("TradeExecutor flush timed out")
            time.sleep(0.01)

    def stop(self, timeout: float = 5.0) -> None:
        """停止执行线程并清空待执行事件，不联动关闭挂单管理。"""
        self._stop_event.set()
        if self._exec_thread is not None:
            self._exec_thread.join(timeout=timeout)
            self._exec_thread = None
        self._clear_exec_queue()

    def shutdown(self, timeout: float = 5.0) -> None:
        """停止后台线程并清理残留执行队列。"""
        self.stop(timeout=timeout)
        if self._pending_manager is not None:
            self._pending_manager.shutdown()

    # ------------------------------------------------------------------
    # Internal execution logic
    # ------------------------------------------------------------------

    def add_trade_listener(self, fn: Callable[[dict], None]) -> None:
        """注册成交监听回调。"""
        self._on_trade_executed.append(fn)

    def remove_trade_listener(self, fn: Callable[[dict], None]) -> None:
        try:
            self._on_trade_executed.remove(fn)
        except ValueError:
            pass

    def _clear_exec_queue(self) -> None:
        while True:
            try:
                self._exec_queue.get_nowait()
                self._exec_queue.task_done()
            except queue.Empty:
                break

    def set_margin_guard(self, guard: Any) -> None:
        """注入保证金保护器。"""
        self._margin_guard = guard

    def reset_circuit(self) -> None:
        # Reset the circuit breaker manually.
        self._circuit_open = False
        self._consecutive_failures = 0
        self._circuit_open_at = None
        logger.info("TradeExecutor: circuit breaker manually reset")

    def _handle_confirmed(self, event: SignalEvent) -> dict[str, Any | None]:
        self._signals_received += 1
        tf = event.timeframe or ""
        if tf:
            with self._skip_lock:
                tf_entry = self._tf_stats.setdefault(
                    tf, {"received": 0, "passed": 0, "skip_reasons": {}}
                )
                tf_entry["received"] += 1
        if not self.config.enabled:
            return None

        # 技术故障熔断器优先于后续所有交易检查。
        if self._circuit_open:
            # T-3: 超过自动恢复窗口后，允许先进入 half-open 再尝试一次。
            if (
                self.config.circuit_auto_reset_minutes > 0
                and self._circuit_open_at is not None
            ):
                elapsed = (
                    datetime.now(timezone.utc) - self._circuit_open_at
                ).total_seconds() / 60.0
                if elapsed >= self.config.circuit_auto_reset_minutes:
                    logger.info(
                        "TradeExecutor: circuit auto-reset after %.1f minutes, "
                        "attempting half-open",
                        elapsed,
                    )
                    self.reset_circuit()
            if self._circuit_open:
                logger.warning(
                    "TradeExecutor: circuit open (consecutive_failures=%d), "
                    "skipping %s/%s. Call reset_circuit() to resume.",
                    self._consecutive_failures, event.symbol, event.strategy,
                )
                return None

        if event.direction not in ("buy", "sell"):
            return None

        # PnL 熔断由日内绩效跟踪器维护，独立于技术故障熔断。
        if (
            self._performance_tracker is not None
            and self._performance_tracker.is_trading_paused()
        ):
            logger.warning(
                "TradeExecutor: PnL circuit open, skipping %s/%s %s",
                event.symbol, event.strategy, event.direction,
            )
            _emit_execution_blocked_helper(
                self,
                event,
                reason="pnl_circuit_paused",
                category="performance",
            )
            _notify_skip_helper(self, event.signal_id, "pnl_circuit_paused", tf)
            return None

        # 保证金保护在真正计算仓位前拦截新开仓请求。
        if self._margin_guard is not None and self._margin_guard.should_block_new_trades():
            logger.info(
                "TradeExecutor: skipping %s/%s %s - margin guard blocked",
                event.symbol, event.strategy, event.direction,
            )
            _emit_execution_blocked_helper(
                self,
                event,
                reason="margin_guard_block",
                category="risk_guard",
            )
            _notify_skip_helper(self, event.signal_id, "margin_guard_block", tf)
            return None

        # 执行门负责策略级准入规则，例如 voting group 和 armed 检查。
        gate_allowed, gate_reason = self._execution_gate.check(event)
        if not gate_allowed:
            logger.info(
                "TradeExecutor: skipping %s/%s %s - gate blocked: %s",
                event.symbol, event.strategy, event.direction, gate_reason,
            )
            _emit_execution_blocked_helper(
                self,
                event,
                reason=gate_reason,
                category="execution_gate",
            )
            _notify_skip_helper(self, event.signal_id, gate_reason, tf)
            return None

        duplicate_reason = _duplicate_execution_reason_helper(self, event)
        if duplicate_reason:
            logger.info(
                "TradeExecutor: skipping %s/%s/%s %s - duplicate execution context: %s",
                event.symbol, tf, event.strategy, event.direction, duplicate_reason,
            )
            self._execution_log.append(
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "signal_id": event.signal_id,
                    "symbol": event.symbol,
                    "direction": event.direction,
                    "strategy": event.strategy,
                    "success": False,
                    "skipped": True,
                    "reason": duplicate_reason,
                }
            )
            _emit_execution_blocked_helper(
                self,
                event,
                reason=duplicate_reason,
                category="duplicate_guard",
            )
            _notify_skip_helper(self, event.signal_id, duplicate_reason, tf)
            return None

        # Per-TF 最小置信度覆盖优先于全局 min_confidence。
        effective_min_conf = self.config.timeframe_min_confidence.get(
            tf, self.config.min_confidence
        )
        if event.confidence < effective_min_conf:
            logger.info(
                "TradeExecutor: skipping %s/%s %s - confidence %.3f < min=%.2f (tf=%s)",
                event.symbol,
                event.strategy,
                event.direction,
                event.confidence,
                effective_min_conf,
                tf,
            )
            _emit_execution_blocked_helper(
                self,
                event,
                reason="min_confidence",
                category="confidence",
            )
            _notify_skip_helper(self, event.signal_id, "min_confidence", tf)
            return None

        # HTF 方向冲突可在低周期直接阻止开仓；默认仅豁免允许逆势的策略类别。
        # 这能避免趋势跟随策略在高周期反向时，仍在低周期继续追价入场。
        strategy_category = event.metadata.get("strategy_category", "")
        if (
            tf in self.config.htf_conflict_block_timeframes
            and event.metadata.get("htf_alignment") == "conflict"
            and strategy_category not in self.config.htf_conflict_exempt_categories
        ):
            logger.info(
                "TradeExecutor: BLOCKING %s/%s/%s %s - HTF direction conflict "
                "on low TF %s (htf_dir=%s, category=%s)",
                event.symbol, tf, event.strategy, event.direction,
                tf, event.metadata.get("htf_direction", "?"), strategy_category,
            )
            _emit_execution_blocked_helper(
                self,
                event,
                reason="htf_conflict_block",
                category="htf_alignment",
            )
            _notify_skip_helper(self, event.signal_id, "htf_conflict_block", tf)
            return None

        # 日终平仓后，当天剩余时间内不再新开仓，避免 EOD 后又被实时信号重新拉起仓位。
        if (
            self._position_manager is not None
            and hasattr(self._position_manager, "is_after_eod_today")
            and self._position_manager.is_after_eod_today()
        ):
            logger.info(
                "TradeExecutor: BLOCKING %s/%s %s - after EOD closeout, no new positions today",
                event.symbol, event.strategy, event.direction,
            )
            _emit_execution_blocked_helper(
                self,
                event,
                reason="after_eod_block",
                category="eod_guard",
            )
            _notify_skip_helper(self, event.signal_id, "after_eod_block", tf)
            return None

        if _reached_position_limit_helper(self, event.symbol):
            logger.info(
                "TradeExecutor: skipping %s/%s %s - max_concurrent_positions_per_symbol reached",
                event.symbol,
                event.strategy,
                event.direction,
            )
            self._execution_log.append(
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "signal_id": event.signal_id,
                    "symbol": event.symbol,
                    "direction": event.direction,
                    "strategy": event.strategy,
                    "success": False,
                    "skipped": True,
                    "reason": "max_concurrent_positions_per_symbol",
                }
            )
            _emit_execution_blocked_helper(
                self,
                event,
                reason="position_limit",
                category="position_limit",
            )
            _notify_skip_helper(self, event.signal_id, "position_limit", tf)
            return None

        # ── 同策略同方向再入场冷却 ────────────────────────────────
        cooldown_bars = self.config.reentry_cooldown_bars
        if cooldown_bars > 0:
            reentry_key = (event.symbol, event.strategy, event.direction)
            bar_time_raw = event.metadata.get("bar_time")
            bar_time: datetime | None = None
            if isinstance(bar_time_raw, datetime):
                bar_time = bar_time_raw
            elif isinstance(bar_time_raw, str):
                try:
                    bar_time = datetime.fromisoformat(bar_time_raw)
                except (ValueError, TypeError):
                    pass
            last_bar = self._last_entry_bar_time.get(reentry_key)
            if bar_time and last_bar:
                tf_seconds = _tf_to_seconds_helper(tf)
                if tf_seconds > 0:
                    elapsed_bars = abs((bar_time - last_bar).total_seconds()) / tf_seconds
                    if elapsed_bars < cooldown_bars:
                        logger.info(
                            "TradeExecutor: skipping %s/%s/%s %s - reentry cooldown "
                            "(%.1f bars < %d required)",
                            event.symbol, tf, event.strategy, event.direction,
                            elapsed_bars, cooldown_bars,
                        )
                        _emit_execution_blocked_helper(
                            self,
                            event,
                            reason="reentry_cooldown",
                            category="cooldown",
                        )
                        _notify_skip_helper(
                            self, event.signal_id, "reentry_cooldown", tf
                        )
                        return None

        trade_params = _compute_params_helper(self, event)
        if trade_params is None:
            atr = extract_atr_from_indicators(event.indicators)
            balance = _get_account_balance_helper(self)
            close_price = event.metadata.get("close_price") or _estimate_price_helper(
                event.indicators
            )
            logger.warning(
                "TradeExecutor: cannot compute trade params for %s/%s %s "
                "(atr=%s, balance=%s, close_price=%s, indicators_keys=%s)",
                event.symbol, event.strategy, event.direction,
                atr, balance, close_price,
                list(event.indicators.keys()),
            )
            _emit_execution_blocked_helper(
                self,
                event,
                reason="trade_params_unavailable",
                category="trade_params",
            )
            _notify_skip_helper(
                self, event.signal_id, "trade_params_unavailable", tf
            )
            return None
        cost_metrics = _estimate_cost_metrics_helper(self, event, trade_params)
        spread_to_stop_ratio = cost_metrics.get("spread_to_stop_ratio")
        if (
            spread_to_stop_ratio is not None
            and spread_to_stop_ratio > self.config.max_spread_to_stop_ratio
        ):
            logger.info(
                "TradeExecutor: skipping %s/%s %s - spread_to_stop_ratio %.3f > max=%.3f",
                event.symbol,
                event.strategy,
                event.direction,
                spread_to_stop_ratio,
                self.config.max_spread_to_stop_ratio,
            )
            self._execution_log.append(
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "signal_id": event.signal_id,
                    "symbol": event.symbol,
                    "direction": event.direction,
                    "strategy": event.strategy,
                    "success": False,
                    "skipped": True,
                    "reason": "spread_to_stop_ratio_too_high",
                    "cost": cost_metrics,
                }
            )
            _emit_execution_blocked_helper(
                self,
                event,
                reason="spread_to_stop_ratio_too_high",
                category="cost_guard",
            )
            _notify_skip_helper(
                self, event.signal_id, "spread_to_stop_ratio_too_high", tf
            )
            return None

        self._signals_passed += 1
        if tf:
            with self._skip_lock:
                self._tf_stats.setdefault(
                    tf, {"received": 0, "passed": 0, "skip_reasons": {}}
                )["passed"] += 1
        _emit_execution_decided_helper(
            self,
            event,
            order_kind="market" if self._pending_manager is None else "pending",
        )
        if self._pending_manager is None:
            return _execute_market_order_helper(
                self, event, trade_params, cost_metrics=cost_metrics
            )
        return _submit_pending_entry_helper(self, event, trade_params, cost_metrics)

    def status(self) -> dict[str, Any]:
        slippage_samples = int(self._execution_quality["slippage_samples"] or 0)
        return {
            "enabled": self.config.enabled,
            "signals_received": self._signals_received,
            "signals_passed": self._signals_passed,
            "signals_blocked": self._signals_received - self._signals_passed,
            "skip_reasons": {k: v for k, v in self._skip_reasons.items()},
            "execution_count": self._execution_count,
            "last_execution_at": self._last_execution_at.isoformat() if self._last_execution_at else None,
            "last_error": self._last_error,
            "last_risk_block": self._last_risk_block,
            "circuit_breaker": {
                "open": self._circuit_open,
                "consecutive_failures": self._consecutive_failures,
                "max_consecutive_failures": self.config.max_consecutive_failures,
                "circuit_open_at": self._circuit_open_at.isoformat() if self._circuit_open_at else None,
                "auto_reset_minutes": self.config.circuit_auto_reset_minutes,
            },
            "config": {
                "min_confidence": self.config.min_confidence,
                "max_concurrent_positions_per_symbol": self.config.max_concurrent_positions_per_symbol,
                "max_spread_to_stop_ratio": self.config.max_spread_to_stop_ratio,
                "risk_percent": self.config.risk_percent,
                "sl_atr_multiplier": self.config.sl_atr_multiplier,
                "tp_atr_multiplier": self.config.tp_atr_multiplier,
            },
            "execution_gate": {
                "require_armed": self._execution_gate.config.require_armed,
                "trade_trigger_strategies": list(
                    getattr(self._execution_gate.config, "trade_trigger_strategies", ())
                ),
                "voting_group_strategies": sorted(self._execution_gate.config.voting_group_strategies),
                "standalone_override": sorted(self._execution_gate.config.standalone_override),
            },
            "execution_quality": {
                "recovered_from_state": int(self._execution_quality["recovered_from_state"] or 0),
                "risk_blocks": int(self._execution_quality["risk_blocks"] or 0),
                "slippage_samples": slippage_samples,
                "avg_slippage_price": round(
                    float(self._execution_quality["slippage_total_price"] or 0.0)
                    / slippage_samples,
                    6,
                ) if slippage_samples else None,
                "avg_slippage_points": round(
                    float(self._execution_quality["slippage_total_points"] or 0.0)
                    / slippage_samples,
                    4,
                ) if slippage_samples else None,
            },
            "by_timeframe": {
                tf: {
                    "received": entry["received"],
                    "passed": entry["passed"],
                    "blocked": entry["received"] - entry["passed"],
                    "skip_reasons": dict(entry["skip_reasons"]),
                }
                for tf, entry in self._tf_stats.items()
            },
            "recent_executions": list(self._execution_log)[-10:],
            "pending_entries": (
                self._pending_manager.status()
                if self._pending_manager is not None
                else None
            ),
        }
