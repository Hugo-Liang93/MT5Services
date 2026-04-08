"""TradeExecutor：消费确认信号并执行自动下单。

职责边界保持清晰：
- 信号模块只负责生成并发布 `SignalEvent`
- `TradeExecutor` 通过 `SignalRuntime.add_signal_listener()` 订阅事件并执行交易

在 `deps.py` 中的典型接入方式：
    executor = TradeExecutor(trading_module=_c.trade_module, config=cfg)
    signal_runtime.add_signal_listener(executor.on_signal_event)
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from src.signals.evaluation.performance import StrategyPerformanceTracker

from src.monitoring.pipeline import PipelineEventBus
from src.signals.metadata_keys import MetadataKey as MK
from src.signals.models import SignalEvent

from ..pending.manager import PendingEntryManager
from ..ports import ExecutorTradingPort
from ..positions.manager import PositionManager
from ..tracking.trade_outcome import TradeOutcomeTracker
from .eventing import emit_execution_blocked as _emit_execution_blocked_helper
from .eventing import emit_execution_decided as _emit_execution_decided_helper
from .eventing import execute_market_order as _execute_market_order_helper
from .eventing import notify_skip as _notify_skip_helper
from .gate import ExecutionGate, ExecutionGateConfig
from .params import compute_params as _compute_params_helper
from .params import estimate_cost_metrics as _estimate_cost_metrics_helper
from .params import estimate_price as _estimate_price_helper
from .params import get_account_balance as _get_account_balance_helper
from .params import tf_to_seconds as _tf_to_seconds_helper
from .pending_orders import (
    duplicate_execution_reason as _duplicate_execution_reason_helper,
)
from .pending_orders import reached_position_limit as _reached_position_limit_helper
from .pending_orders import submit_pending_entry as _submit_pending_entry_helper
from .sizing import RegimeSizing, extract_atr_from_indicators

logger = logging.getLogger(__name__)


@dataclass
class ExecutorConfig:
    enabled: bool = False
    min_confidence: float = 0.7
    # 按时间框架覆盖最低交易置信度，低周期可以设置更严格阈值。
    timeframe_min_confidence: dict[str, float] = field(default_factory=dict)
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
        equity_curve_filter: Any | None = None,
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
        # 权益曲线过滤器。
        self._equity_curve_filter = equity_curve_filter
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
        self._margin_guard: Any = (
            None  # Optional[MarginGuard], injected via set_margin_guard()
        )
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
        # 健康检查连续失败计数，防止 auto-reset 死循环。
        self._health_check_failures: int = 0
        self._MAX_HEALTH_CHECK_FAILURES: int = 5
        # 同策略同方向再入场冷却：记录上次开仓的 bar_time
        # key = (symbol, strategy, direction), value = bar_time(datetime)
        self._last_entry_bar_time: dict[tuple[str, str, str], datetime] = {}
        # signal_id 幂等性保护：已执行的 signal_id 有界缓存，防止重复下单
        self._executed_signal_ids: deque[str] = deque(maxlen=2000)
        # Intrabar 交易去重 + confirmed 协调
        self._intrabar_guard: Any | None = None  # IntrabarTradeGuard, injected
        # Intrabar 执行统计
        self._intrabar_signals_received: int = 0
        self._intrabar_signals_executed: int = 0

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
                event.symbol,
                event.timeframe,
                event.strategy,
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
                    event.symbol,
                    event.timeframe,
                    event.strategy,
                    sig_id,
                    self._execution_quality["queue_overflows"],
                )
                # 保留最近一批被丢弃的 confirmed 信号，便于通过 status() 排查。
                dropped_entry = {
                    "signal_id": sig_id,
                    "symbol": event.symbol,
                    "timeframe": event.timeframe,
                    "strategy": event.strategy,
                    "direction": getattr(event, "direction", ""),
                    "confidence": getattr(event, "confidence", 0.0),
                    "dropped_at": time.time(),
                }
                self._dropped_signals.append(dropped_entry)
                if len(self._dropped_signals) > self._max_dropped_history:
                    self._dropped_signals = self._dropped_signals[
                        -self._max_dropped_history :
                    ]
                # 持久化到执行日志，确保重启后可追溯丢失的信号
                if self._persist_execution_fn is not None:
                    try:
                        self._persist_execution_fn(
                            [
                                {
                                    "at": datetime.now(timezone.utc).isoformat(),
                                    "signal_id": sig_id,
                                    "symbol": event.symbol,
                                    "timeframe": event.timeframe,
                                    "direction": getattr(event, "direction", ""),
                                    "strategy": event.strategy,
                                    "confidence": getattr(event, "confidence", 0.0),
                                    "success": False,
                                    "dropped": True,
                                    "reason": "queue_overflow",
                                }
                            ]
                        )
                    except Exception:
                        logger.debug("Failed to persist dropped signal", exc_info=True)

    def on_intrabar_trade_signal(self, event: SignalEvent) -> None:
        """Intrabar 交易信号入口。仅接受 intrabar_armed_* 状态。

        与 on_signal_event() 并行注册为 signal listener。
        intrabar 是 best-effort，队列满时直接丢弃。
        """
        if event.scope != "intrabar":
            return
        if not event.signal_state.startswith("intrabar_armed_"):
            return
        if not event.signal_id:
            return
        self._intrabar_signals_received += 1
        if self._exec_thread is None or not self._exec_thread.is_alive():
            self._start_worker()
        try:
            self._exec_queue.put_nowait(event)
        except queue.Full:
            logger.warning(
                "TradeExecutor: exec queue full, dropping intrabar trade signal "
                "%s/%s/%s",
                event.symbol,
                event.timeframe,
                event.strategy,
            )

    def _start_worker(self) -> None:
        """启动后台执行线程。"""
        # 防止双线程：旧线程仍活着时复用，不创建新线程
        if self._exec_thread is not None and self._exec_thread.is_alive():
            return
        self._stop_event.clear()
        t = threading.Thread(
            target=self._exec_worker, name="trade-executor", daemon=True
        )
        t.start()
        self._exec_thread = t

    def start(self) -> None:
        """启用执行器，允许后续 confirmed 事件重新拉起执行线程。"""
        # 等待上一次 stop() 超时遗留的僵尸线程退出
        old = self._exec_thread
        if old is not None and old.is_alive():
            logger.warning("TradeExecutor: waiting for previous worker to finish")
            old.join(timeout=5.0)
            if old.is_alive():
                logger.error(
                    "TradeExecutor: previous worker still alive after re-join, "
                    "new signals will reuse it instead of creating a new thread"
                )
        self._stop_event.clear()

    def is_running(self) -> bool:
        return self._exec_thread is not None and self._exec_thread.is_alive()

    def _exec_worker(self) -> None:
        """后台消费执行队列，串行处理 confirmed 和 intrabar 交易事件。"""
        while not self._stop_event.is_set() or not self._exec_queue.empty():
            try:
                event = self._exec_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                if event.scope == "intrabar":
                    self._handle_intrabar_entry(event)
                else:
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
            if self._exec_thread.is_alive():
                logger.warning(
                    "TradeExecutor worker did not stop within %.1fs, "
                    "will be cleaned up on next start()",
                    timeout,
                )
                # 保留引用，start() 会检测并等待
            else:
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
        """Reset the circuit breaker manually."""
        self._circuit_open = False
        self._consecutive_failures = 0
        self._circuit_open_at = None
        self._health_check_failures = 0
        logger.info("TradeExecutor: circuit breaker manually reset")

    def _check_trading_health(self) -> bool:
        """熔断器自动恢复前的健康检查：验证 MT5 连接和账户可用。"""
        try:
            info = self._trading.account_info()
            return info is not None
        except Exception as exc:
            logger.debug("Trading health check failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Pre-trade filter chain
    # ------------------------------------------------------------------

    def _reject_signal(
        self,
        event: SignalEvent,
        reason: str,
        category: str,
        tf: str,
        *,
        log_level: str = "info",
        extra_log: str = "",
        pipeline_reason: str = "",
    ) -> None:
        """统一拒绝信号：日志 + 事件总线 + skip 通知 + 执行日志。

        Args:
            reason: 写入 execution_log 和 skip 通知的详细拒绝原因。
            category: 事件总线分类。
            pipeline_reason: 事件总线的 reason（空则用 reason）。
        """
        msg = (
            f"TradeExecutor: skipping {event.symbol}/{event.strategy} "
            f"{event.direction} - {reason}"
        )
        if extra_log:
            msg = f"{msg} ({extra_log})"
        getattr(logger, log_level)(msg)
        self._execution_log.append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "signal_id": event.signal_id,
                "symbol": event.symbol,
                "direction": event.direction,
                "strategy": event.strategy,
                "success": False,
                "skipped": True,
                "reason": reason,
            }
        )
        _emit_execution_blocked_helper(
            self,
            event,
            reason=pipeline_reason or reason,
            category=category,
        )
        _notify_skip_helper(self, event.signal_id, reason, tf, event=event)

    def _check_circuit_breaker(self, event: SignalEvent) -> bool:
        """检查技术故障熔断器。返回 True = 已熔断，应拒绝。"""
        if not self._circuit_open:
            return False
        # 超过自动恢复窗口后，先做健康检查再决定是否 half-open。
        if (
            self.config.circuit_auto_reset_minutes > 0
            and self._circuit_open_at is not None
        ):
            elapsed = (
                datetime.now(timezone.utc) - self._circuit_open_at
            ).total_seconds() / 60.0
            if elapsed >= self.config.circuit_auto_reset_minutes:
                if self._check_trading_health():
                    logger.info(
                        "TradeExecutor: circuit auto-reset after %.1f minutes, "
                        "health check passed, entering half-open",
                        elapsed,
                    )
                    self._health_check_failures = 0
                    self.reset_circuit()
                else:
                    self._health_check_failures += 1
                    self._circuit_open_at = datetime.now(timezone.utc)
                    if self._health_check_failures >= self._MAX_HEALTH_CHECK_FAILURES:
                        logger.critical(
                            "TradeExecutor: circuit STUCK — %d consecutive health "
                            "check failures, MT5 connection may be permanently "
                            "broken. Manual intervention required (reset_circuit "
                            "or restart).",
                            self._health_check_failures,
                        )
                    else:
                        logger.warning(
                            "TradeExecutor: circuit auto-reset deferred, "
                            "health check failed after %.1f minutes "
                            "(health_check_failures=%d/%d)",
                            elapsed,
                            self._health_check_failures,
                            self._MAX_HEALTH_CHECK_FAILURES,
                        )
        if self._circuit_open:
            logger.warning(
                "TradeExecutor: circuit open (consecutive_failures=%d), "
                "skipping %s/%s. Call reset_circuit() to resume.",
                self._consecutive_failures,
                event.symbol,
                event.strategy,
            )
            return True
        return False

    def _check_reentry_cooldown(self, event: SignalEvent, tf: str) -> bool:
        """检查同策略同方向再入场冷却。返回 True = 冷却中，应拒绝。"""
        cooldown_bars = self.config.reentry_cooldown_bars
        if cooldown_bars <= 0:
            return False
        reentry_key = (event.symbol, event.strategy, event.direction)
        bar_time_raw = event.metadata.get(MK.BAR_TIME)
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
                    self._reject_signal(
                        event,
                        "reentry_cooldown",
                        "cooldown",
                        tf,
                        extra_log=f"{elapsed_bars:.1f} bars < {cooldown_bars} required",
                    )
                    return True
        return False

    def _run_pre_trade_filters(self, event: SignalEvent, tf: str) -> str | None:
        """按顺序运行所有预交易过滤器。

        返回 None = 全部通过；返回 str = 拒绝原因。
        """
        sig_id = event.signal_id or ""

        # ① signal_id 幂等性
        if sig_id and sig_id in self._executed_signal_ids:
            _notify_skip_helper(self, sig_id, "duplicate_signal_id", tf, event=event)
            return "duplicate_signal_id"

        # ② 技术故障熔断器
        if self._check_circuit_breaker(event):
            return "circuit_open"

        # ③ 方向有效性
        if event.direction not in ("buy", "sell"):
            return "invalid_direction"

        # ④ PnL 熔断
        if (
            self._performance_tracker is not None
            and self._performance_tracker.is_trading_paused()
        ):
            self._reject_signal(
                event,
                "pnl_circuit_paused",
                "performance",
                tf,
                log_level="warning",
            )
            return "pnl_circuit_paused"

        # ⑤ 权益曲线过滤器
        if self._equity_curve_filter is not None:
            self._equity_curve_filter.record_equity()
            if self._equity_curve_filter.should_block():
                self._reject_signal(
                    event,
                    "equity_curve_below_ma",
                    "equity_filter",
                    tf,
                )
                return "equity_curve_below_ma"

        # ⑥ 保证金保护
        if (
            self._margin_guard is not None
            and self._margin_guard.should_block_new_trades()
        ):
            self._reject_signal(event, "margin_guard_block", "risk_guard", tf)
            return "margin_guard_block"

        # ⑦ 执行门（voting group / armed）
        gate_allowed, gate_reason = self._execution_gate.check(event)
        if not gate_allowed:
            self._reject_signal(event, gate_reason, "execution_gate", tf)
            return gate_reason

        # ⑧ 重复执行上下文
        duplicate_reason = _duplicate_execution_reason_helper(self, event)
        if duplicate_reason:
            self._reject_signal(event, duplicate_reason, "duplicate_guard", tf)
            return duplicate_reason

        # ⑨ 最小置信度
        effective_min_conf = self.config.timeframe_min_confidence.get(
            tf,
            self.config.min_confidence,
        )
        if event.confidence < effective_min_conf:
            self._reject_signal(
                event,
                "min_confidence",
                "confidence",
                tf,
                extra_log=f"{event.confidence:.3f} < {effective_min_conf:.2f}",
            )
            return "min_confidence"

        # ⑩ EOD 后禁止新开仓
        if (
            self._position_manager is not None
            and hasattr(self._position_manager, "is_after_eod_today")
            and self._position_manager.is_after_eod_today()
        ):
            self._reject_signal(event, "after_eod_block", "eod_guard", tf)
            return "after_eod_block"

        # ⑪ 品种持仓数量上限
        if _reached_position_limit_helper(self, event.symbol):
            self._reject_signal(
                event,
                "max_concurrent_positions_per_symbol",
                "position_limit",
                tf,
                pipeline_reason="position_limit",
            )
            return "max_concurrent_positions_per_symbol"

        # ⑫ 再入场冷却
        if self._check_reentry_cooldown(event, tf):
            return "reentry_cooldown"

        return None

    # ------------------------------------------------------------------
    # Core execution dispatch
    # ------------------------------------------------------------------

    def _handle_confirmed(self, event: SignalEvent) -> dict[str, Any | None]:
        self._signals_received += 1
        tf = event.timeframe or ""
        if tf:
            with self._skip_lock:
                tf_entry = self._tf_stats.setdefault(
                    tf, {"received": 0, "passed": 0, "skip_reasons": {}}
                )
                tf_entry["received"] += 1

        # ── Intrabar 仓位协调：confirmed 验证 ──
        if self._intrabar_guard is not None:
            bar_time = event.metadata.get(MK.BAR_TIME)
            has_pos, intra_dir = self._intrabar_guard.has_intrabar_position(
                event.symbol,
                event.timeframe,
                event.strategy,
                bar_time,
            )
            if has_pos:
                if event.direction == intra_dir:
                    # 同方向 → 仓位继续，跳过
                    logger.info(
                        "Intrabar position validated by confirmed signal: "
                        "%s/%s/%s %s",
                        event.symbol,
                        event.timeframe,
                        event.strategy,
                        intra_dir,
                    )
                    return None
                elif event.direction not in ("buy", "sell"):
                    # hold → 不动，交给出场规则
                    logger.info(
                        "Confirmed hold with intrabar position, keeping: " "%s/%s/%s",
                        event.symbol,
                        event.timeframe,
                        event.strategy,
                    )
                    return None
                # 反转 → fall through 到正常 confirmed 流程（开反向仓）
                # PositionManager 的 Chandelier Exit / 信号反转确认会处理平仓
                logger.info(
                    "Confirmed reversal with intrabar position: "
                    "%s/%s/%s intrabar=%s confirmed=%s",
                    event.symbol,
                    event.timeframe,
                    event.strategy,
                    intra_dir,
                    event.direction,
                )
            # 清理该 bar 的 guard 状态
            if bar_time is not None:
                self._intrabar_guard.on_parent_bar_close(
                    event.symbol,
                    event.timeframe,
                    bar_time,
                )

        if not self.config.enabled:
            return None

        # ── 预交易过滤链 ──
        reject_reason = self._run_pre_trade_filters(event, tf)
        if reject_reason is not None:
            return None

        # ── 交易参数计算 ──
        trade_params = _compute_params_helper(self, event)
        if trade_params is None:
            atr = extract_atr_from_indicators(event.indicators)
            balance = _get_account_balance_helper(self)
            close_price = event.metadata.get(MK.CLOSE_PRICE) or _estimate_price_helper(
                event.indicators
            )
            logger.warning(
                "TradeExecutor: cannot compute trade params for %s/%s %s "
                "(atr=%s, balance=%s, close_price=%s, indicators_keys=%s)",
                event.symbol,
                event.strategy,
                event.direction,
                atr,
                balance,
                close_price,
                list(event.indicators.keys()),
            )
            self._reject_signal(
                event,
                "trade_params_unavailable",
                "trade_params",
                tf,
            )
            return None

        # ── Spread-to-stop 比率检查 ──
        cost_metrics = _estimate_cost_metrics_helper(self, event, trade_params)
        spread_to_stop_ratio = cost_metrics.get("spread_to_stop_ratio")
        if (
            spread_to_stop_ratio is not None
            and spread_to_stop_ratio > self.config.max_spread_to_stop_ratio
        ):
            self._reject_signal(
                event,
                "spread_to_stop_ratio_too_high",
                "cost_guard",
                tf,
                extra_log=f"{spread_to_stop_ratio:.3f} > {self.config.max_spread_to_stop_ratio:.3f}",
            )
            return None

        # ── 全部通过，派发执行 ──
        sig_id = event.signal_id or ""
        self._signals_passed += 1
        if sig_id:
            self._executed_signal_ids.append(sig_id)
        if tf:
            with self._skip_lock:
                self._tf_stats.setdefault(
                    tf, {"received": 0, "passed": 0, "skip_reasons": {}}
                )["passed"] += 1
        entry_spec = event.metadata.get(MK.ENTRY_SPEC, {})
        entry_type = entry_spec.get("entry_type", "market")
        use_market = entry_type == "market" or self._pending_manager is None

        _emit_execution_decided_helper(
            self,
            event,
            order_kind="market" if use_market else entry_type,
        )
        if use_market:
            return _execute_market_order_helper(
                self, event, trade_params, cost_metrics=cost_metrics
            )
        return _submit_pending_entry_helper(self, event, trade_params, cost_metrics)

    # ------------------------------------------------------------------
    # Intrabar 交易执行
    # ------------------------------------------------------------------

    def _handle_intrabar_entry(self, event: SignalEvent) -> None:
        """Intrabar armed 信号执行。复用完整 pre-trade filter chain。"""
        if not self.config.enabled:
            return
        if self._intrabar_guard is None:
            logger.warning(
                "TradeExecutor: intrabar guard not injected, "
                "dropping intrabar signal %s/%s/%s",
                event.symbol,
                event.timeframe,
                event.strategy,
            )
            return

        # 1. 去重检查
        parent_bar_time = event.parent_bar_time or event.metadata.get(
            MK.INTRABAR_PARENT_BAR_TIME
        )
        if parent_bar_time is None:
            logger.warning("TradeExecutor: no parent_bar_time in intrabar event, skip")
            return
        allowed, reason = self._intrabar_guard.can_trade(
            event.symbol,
            event.timeframe,
            event.strategy,
            event.direction,
            parent_bar_time,
        )
        if not allowed:
            logger.debug(
                "IntrabarTradeGuard blocked: %s/%s/%s reason=%s",
                event.symbol,
                event.timeframe,
                event.strategy,
                reason,
            )
            return

        # 2. ExecutionGate intrabar 检查
        gate_ok, gate_reason = self._execution_gate.check_intrabar(event)
        if not gate_ok:
            logger.debug(
                "ExecutionGate.check_intrabar blocked: %s/%s/%s reason=%s",
                event.symbol,
                event.timeframe,
                event.strategy,
                gate_reason,
            )
            return

        # 3. Pre-trade filter chain（复用完整 12 层）
        tf = event.timeframe or ""
        reject_reason = self._run_pre_trade_filters(event, tf)
        if reject_reason is not None:
            return

        # 4. 交易参数计算（ATR 用上一根确认 bar 的）
        trade_params = _compute_params_helper(self, event)
        if trade_params is None:
            logger.warning(
                "TradeExecutor: cannot compute params for intrabar entry " "%s/%s/%s",
                event.symbol,
                event.timeframe,
                event.strategy,
            )
            return

        # 5. 成本检查
        cost_metrics = _estimate_cost_metrics_helper(self, event, trade_params)
        if cost_metrics is not None and cost_metrics.get("blocked"):
            logger.info(
                "Intrabar entry blocked by cost check: %s/%s/%s",
                event.symbol,
                event.timeframe,
                event.strategy,
            )
            return

        # 6. 执行（复用现有 market/pending 逻辑）
        entry_spec = event.metadata.get(MK.ENTRY_SPEC, {})
        entry_type = entry_spec.get("entry_type", "market") if entry_spec else "market"

        if entry_type == "market" or self._pending_manager is None:
            execute_market_order(self, event, trade_params, cost_metrics or {})
        else:
            _submit_pending_entry_helper(self, event, trade_params, cost_metrics or {})

        # 7. 记录到 guard
        self._intrabar_guard.record_trade(
            event.symbol,
            event.timeframe,
            event.strategy,
            event.direction,
            parent_bar_time,
        )
        self._intrabar_signals_executed += 1
        logger.info(
            "Intrabar trade executed: %s/%s/%s %s (confidence=%.3f)",
            event.symbol,
            event.timeframe,
            event.strategy,
            event.direction,
            event.confidence,
        )

    def status(self) -> dict[str, Any]:
        slippage_samples = int(self._execution_quality["slippage_samples"] or 0)
        return {
            "enabled": self.config.enabled,
            "signals_received": self._signals_received,
            "signals_passed": self._signals_passed,
            "signals_blocked": self._signals_received - self._signals_passed,
            "skip_reasons": {k: v for k, v in self._skip_reasons.items()},
            "execution_count": self._execution_count,
            "last_execution_at": (
                self._last_execution_at.isoformat() if self._last_execution_at else None
            ),
            "last_error": self._last_error,
            "last_risk_block": self._last_risk_block,
            "circuit_breaker": {
                "open": self._circuit_open,
                "consecutive_failures": self._consecutive_failures,
                "max_consecutive_failures": self.config.max_consecutive_failures,
                "circuit_open_at": (
                    self._circuit_open_at.isoformat() if self._circuit_open_at else None
                ),
                "auto_reset_minutes": self.config.circuit_auto_reset_minutes,
                "health_check_failures": self._health_check_failures,
                "max_health_check_failures": self._MAX_HEALTH_CHECK_FAILURES,
            },
            "equity_curve_filter": (
                self._equity_curve_filter.status()
                if self._equity_curve_filter is not None
                else {"enabled": False}
            ),
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
                "voting_group_strategies": sorted(
                    self._execution_gate.config.voting_group_strategies
                ),
                "standalone_override": sorted(
                    self._execution_gate.config.standalone_override
                ),
            },
            "execution_quality": {
                "recovered_from_state": int(
                    self._execution_quality["recovered_from_state"] or 0
                ),
                "risk_blocks": int(self._execution_quality["risk_blocks"] or 0),
                "slippage_samples": slippage_samples,
                "avg_slippage_price": (
                    round(
                        float(self._execution_quality["slippage_total_price"] or 0.0)
                        / slippage_samples,
                        6,
                    )
                    if slippage_samples
                    else None
                ),
                "avg_slippage_points": (
                    round(
                        float(self._execution_quality["slippage_total_points"] or 0.0)
                        / slippage_samples,
                        4,
                    )
                    if slippage_samples
                    else None
                ),
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
