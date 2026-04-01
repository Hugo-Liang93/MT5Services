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
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from src.signals.evaluation.performance import StrategyPerformanceTracker

from src.trading.sizing import (
    RegimeSizing,
    TradeParameters,
    compute_trade_params,
    extract_atr_from_indicators,
)
from src.signals.models import SignalEvent
from src.risk.service import PreTradeRiskBlockedError
from src.trading.execution_gate import ExecutionGate, ExecutionGateConfig
from src.trading.pending_entry import (
    PendingEntry,
    PendingEntryConfig,
    PendingEntryManager,
    compute_entry_zone,
    compute_timeout,
    _CATEGORY_ZONE_MODE,
)
from src.trading.position_manager import PositionManager
from src.trading.trade_outcome_tracker import TradeOutcomeTracker

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
        trading_module: Any,
        config: ExecutorConfig | None = None,
        account_balance_getter: Any | None = None,
        position_manager: PositionManager | None = None,
        persist_execution_fn: Callable[[list], None] | None = None,
        trade_outcome_tracker: TradeOutcomeTracker | None = None,
        on_execution_skip: Callable[[str, str], None] | None = None,
        execution_gate: ExecutionGate | None = None,
        pending_entry_manager: PendingEntryManager | None = None,
        performance_tracker: "StrategyPerformanceTracker | None" = None,
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

    def shutdown(self, timeout: float = 5.0) -> None:
        """停止后台线程并清理残留执行队列。"""
        self._stop_event.set()
        if self._pending_manager is not None:
            self._pending_manager.shutdown()
        if self._exec_thread is not None:
            self._exec_thread.join(timeout=timeout)
            self._exec_thread = None
        self._clear_exec_queue()

    # ------------------------------------------------------------------
    # Internal execution logic
    # ------------------------------------------------------------------

    def _notify_skip(self, signal_id: str, reason: str, timeframe: str = "") -> None:
        # Notify downstream components that a signal was skipped.
        with self._skip_lock:
            self._skip_reasons[reason] = self._skip_reasons.get(reason, 0) + 1
            if timeframe:
                tf_entry = self._tf_stats.setdefault(
                    timeframe, {"received": 0, "passed": 0, "skip_reasons": {}}
                )
                tf_entry["skip_reasons"][reason] = tf_entry["skip_reasons"].get(reason, 0) + 1
        if self._on_execution_skip is not None and signal_id:
            try:
                self._on_execution_skip(signal_id, reason)
            except Exception:
                logger.debug("on_execution_skip callback failed", exc_info=True)

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

    def _get_contract_size(self, symbol: str) -> float:
        # Return the configured contract size for a symbol.
        size_map = self.config.contract_size_map
        return size_map.get(symbol, size_map.get("default", 100.0))

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
            self._notify_skip(event.signal_id, "pnl_circuit_paused", tf)
            return None

        # 保证金保护在真正计算仓位前拦截新开仓请求。
        if self._margin_guard is not None and self._margin_guard.should_block_new_trades():
            logger.info(
                "TradeExecutor: skipping %s/%s %s - margin guard blocked",
                event.symbol, event.strategy, event.direction,
            )
            self._notify_skip(event.signal_id, "margin_guard_block", tf)
            return None

        # 执行门负责策略级准入规则，例如 voting group 和 armed 检查。
        gate_allowed, gate_reason = self._execution_gate.check(event)
        if not gate_allowed:
            logger.info(
                "TradeExecutor: skipping %s/%s %s - gate blocked: %s",
                event.symbol, event.strategy, event.direction, gate_reason,
            )
            self._notify_skip(event.signal_id, gate_reason, tf)
            return None

        duplicate_reason = self._duplicate_execution_reason(event)
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
            self._notify_skip(event.signal_id, duplicate_reason, tf)
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
            self._notify_skip(event.signal_id, "min_confidence", tf)
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
            self._notify_skip(event.signal_id, "htf_conflict_block", tf)
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
            self._notify_skip(event.signal_id, "after_eod_block", tf)
            return None

        if self._reached_position_limit(event.symbol):
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
            self._notify_skip(event.signal_id, "position_limit", tf)
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
                tf_seconds = self._tf_to_seconds(tf)
                if tf_seconds > 0:
                    elapsed_bars = abs((bar_time - last_bar).total_seconds()) / tf_seconds
                    if elapsed_bars < cooldown_bars:
                        logger.info(
                            "TradeExecutor: skipping %s/%s/%s %s - reentry cooldown "
                            "(%.1f bars < %d required)",
                            event.symbol, tf, event.strategy, event.direction,
                            elapsed_bars, cooldown_bars,
                        )
                        self._notify_skip(event.signal_id, "reentry_cooldown", tf)
                        return None

        trade_params = self._compute_params(event)
        if trade_params is None:
            atr = extract_atr_from_indicators(event.indicators)
            balance = self._get_account_balance()
            close_price = event.metadata.get("close_price") or self._estimate_price(event.indicators)
            logger.warning(
                "TradeExecutor: cannot compute trade params for %s/%s %s "
                "(atr=%s, balance=%s, close_price=%s, indicators_keys=%s)",
                event.symbol, event.strategy, event.direction,
                atr, balance, close_price,
                list(event.indicators.keys()),
            )
            self._notify_skip(event.signal_id, "trade_params_unavailable", tf)
            return None
        cost_metrics = self._estimate_cost_metrics(event, trade_params)
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
            self._notify_skip(event.signal_id, "spread_to_stop_ratio_too_high", tf)
            return None

        self._signals_passed += 1
        if tf:
            with self._skip_lock:
                self._tf_stats.setdefault(
                    tf, {"received": 0, "passed": 0, "skip_reasons": {}}
                )["passed"] += 1
        if self._pending_manager is None:
            return self._execute(event, trade_params, cost_metrics=cost_metrics)
        return self._submit_pending_entry(event, trade_params, cost_metrics)

    def _submit_pending_entry(
        self,
        event: SignalEvent,
        params: TradeParameters,
        cost_metrics: dict[str, float | None],
    ) -> dict[str, Any | None]:
        """通过 MT5 限价/止损挂单实现价格确认入场。

        根据 zone_mode 决定挂单类型：
          - pullback: LIMIT 单（等回调到有利价位）
          - momentum: STOP 单（等突破到目标价位）
          - symmetric: LIMIT 单（以参考价为中心）
        """
        if not event.signal_id:
            logger.warning(
                "TradeExecutor: cannot submit pending entry without signal_id for %s/%s",
                event.symbol, event.strategy,
            )
            self._notify_skip(event.signal_id, "missing_signal_id", event.timeframe or "")
            return None

        category = event.metadata.get("category", "")
        zone_mode = _CATEGORY_ZONE_MODE.get(category, "symmetric")

        config = self._pending_manager.config
        entry_low, entry_high = compute_entry_zone(
            action=event.direction,
            close_price=params.entry_price,
            atr=params.atr_value,
            zone_mode=zone_mode,
            config=config,
            strategy_name=event.strategy,
            category=category,
            indicators=event.indicators,
        )
        timeout = compute_timeout(event.timeframe, config, category=category)

        # ── 确定挂单类型和价格 ──
        order_kind, trigger_price = self._resolve_pending_order(
            direction=event.direction,
            zone_mode=zone_mode,
            entry_low=entry_low,
            entry_high=entry_high,
        )

        # SL/TP 按挂单价偏移（相对于原始 entry_price 的差值）
        price_shift = trigger_price - params.entry_price
        adjusted_sl = round(params.stop_loss + price_shift, 2)
        adjusted_tp = round(params.take_profit + price_shift, 2)

        # 新信号到来时，取消同品种旧挂单
        if config.cancel_on_new_signal:
            exclude = event.direction if not config.cancel_same_direction else None
            self._pending_manager.cancel_by_symbol(
                event.symbol,
                reason="new_signal_override",
                exclude_direction=exclude,
            )

        # ── 向 MT5 发送限价/止损挂单 ──
        tf = event.timeframe or ""
        payload = {
            "symbol": event.symbol,
            "volume": params.position_size,
            "side": event.direction,
            "order_kind": order_kind,
            "price": trigger_price,
            "sl": adjusted_sl,
            "tp": adjusted_tp,
            "comment": f"{tf}:{event.strategy}:{order_kind}"[:31],
            "request_id": event.signal_id,
            "metadata": self._build_trade_metadata(event),
        }

        try:
            result = self._trading.dispatch_operation("trade", payload)
            order_ticket = None
            if isinstance(result, dict):
                order_ticket = result.get("order") or result.get("ticket")

            logger.info(
                "TradeExecutor: placed %s %s %s @ %.2f (zone=[%.2f,%.2f] mode=%s) "
                "sl=%.2f tp=%.2f ticket=%s",
                order_kind, event.direction, event.symbol, trigger_price,
                entry_low, entry_high, zone_mode,
                adjusted_sl, adjusted_tp, order_ticket,
            )

            # 注册到 PendingEntryManager 追踪生命周期（超时取消）
            if order_ticket is not None:
                self._pending_manager.track_mt5_order(
                    signal_id=event.signal_id,
                    order_ticket=order_ticket,
                    expires_at=datetime.now(timezone.utc) + timeout,
                    direction=event.direction,
                    symbol=event.symbol,
                    strategy=event.strategy,
                )

            self._execution_log.append({
                "at": datetime.now(timezone.utc).isoformat(),
                "signal_id": event.signal_id,
                "symbol": event.symbol,
                "direction": event.direction,
                "strategy": event.strategy,
                "success": True,
                "pending": True,
                "order_kind": order_kind,
                "trigger_price": trigger_price,
                "order_ticket": order_ticket,
            })
            return result

        except Exception as exc:
            logger.error(
                "TradeExecutor: failed to place %s order for %s/%s: %s",
                order_kind, event.symbol, event.strategy, exc,
            )
            self._notify_skip(event.signal_id, f"pending_order_failed:{exc}", tf)
            return None

    @staticmethod
    def _resolve_pending_order(
        direction: str,
        zone_mode: str,
        entry_low: float,
        entry_high: float,
    ) -> tuple[str, float]:
        """根据 zone_mode 和方向确定 MT5 挂单类型和触发价格。

        Returns:
            (order_kind, trigger_price)
            order_kind: "limit" 或 "stop"
            trigger_price: 挂单价格
        """
        if zone_mode == "momentum":
            # 动量追突破：BUY STOP（价格上破）/ SELL STOP（价格下破）
            if direction == "buy":
                return "stop", entry_high  # 价格涨过 entry_high 触发
            else:
                return "stop", entry_low   # 价格跌破 entry_low 触发
        else:
            # pullback / symmetric：LIMIT 单（等回调）
            if direction == "buy":
                return "limit", entry_low   # 等价格回调到 entry_low 买入
            else:
                return "limit", entry_high  # 等价格回调到 entry_high 卖出

    def _compute_params(self, event: SignalEvent) -> TradeParameters | None:
        atr = extract_atr_from_indicators(event.indicators)
        if atr is None or atr <= 0:
            return None

        balance = self._get_account_balance()
        if balance is None or balance <= 0:
            return None

        # T-1: 优先使用 runtime 透传的 close_price，缺失时再从指标快照估算。
        close_price: float | None = None
        raw_close = event.metadata.get("close_price")
        if raw_close is not None:
            try:
                close_price = float(raw_close)
            except (TypeError, ValueError):
                close_price = None
        # fallback 到 payload/指标快照中的价格型字段。
        if close_price is None or close_price <= 0:
            close_price = self._estimate_price(event.indicators)
        if close_price is None or close_price <= 0:
            return None

        # T-2: 合约大小参与最终仓位与盈亏距离计算。
        contract_size = self._get_contract_size(event.symbol)

        return compute_trade_params(
            action=event.direction,
            current_price=close_price,
            atr_value=atr,
            account_balance=balance,
            timeframe=event.timeframe,
            risk_percent=self.config.risk_percent,
            sl_atr_multiplier=self.config.sl_atr_multiplier,
            tp_atr_multiplier=self.config.tp_atr_multiplier,
            min_volume=self.config.min_volume,
            max_volume=self.config.max_volume,
            contract_size=contract_size,
            timeframe_risk_overrides=self.config.timeframe_risk_multipliers or None,
            regime=str(
                event.metadata.get("regime")
                or event.metadata.get("_regime")
                or ""
            ),
            regime_sizing=self.config.regime_sizing,
        )

    def _get_account_balance(self) -> float | None:
        if self._account_balance_getter is not None:
            try:
                return float(self._account_balance_getter())
            except (TypeError, ValueError, AttributeError):
                logger.debug("account_balance_getter failed", exc_info=True)
        try:
            info = self._trading.account_info()
            if isinstance(info, dict):
                return float(info.get("equity") or info.get("balance") or 0)
            return float(getattr(info, "equity", None) or getattr(info, "balance", None) or 0)
        except (TypeError, ValueError, AttributeError) as exc:
            logger.debug("Failed to get account balance: %s", exc)
            return None

    @staticmethod
    def _tf_to_seconds(tf: str) -> int:
        """Convert timeframe string to seconds (e.g. 'M15' → 900)."""
        tf = tf.upper().strip()
        _map = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800,
                "H1": 3600, "H4": 14400, "D1": 86400}
        return _map.get(tf, 0)

    @staticmethod
    def _estimate_price(indicators: dict[str, dict[str, Any]]) -> float | None:
        for name in ("bollinger20", "sma20", "close", "price"):
            payload = indicators.get(name)
            if isinstance(payload, dict):
                for fld in ("close", "value", "last", "bb_mid", "sma"):
                    val = payload.get(fld)
                    if val is not None:
                        try:
                            return float(val)
                        except (TypeError, ValueError):
                            continue
        return None

    def _reached_position_limit(self, symbol: str) -> bool:
        limit = self.config.max_concurrent_positions_per_symbol
        if limit is None or limit <= 0:
            return False
        open_positions = self._open_positions_for_symbol(symbol)
        return open_positions >= limit

    def _open_positions_for_symbol(self, symbol: str) -> int:
        tracked_count: int | None = None
        if self._position_manager is not None:
            try:
                tracked = [
                    row
                    for row in self._position_manager.active_positions()
                    if row.get("symbol") == symbol
                ]
                tracked_count = len(tracked)
            except (TypeError, AttributeError) as exc:
                logger.debug("Failed to count tracked positions: %s", exc)
                tracked_count = None

        for attr_name in ("get_positions", "positions"):
            getter = getattr(self._trading, attr_name, None)
            if not callable(getter):
                continue
            try:
                rows = getter(symbol=symbol)
            except TypeError:
                rows = getter(symbol)
            except Exception:
                continue
            try:
                live_count = len(list(rows or []))
                if tracked_count is None:
                    return live_count
                return max(tracked_count, live_count)
            except Exception:
                continue
        return tracked_count or 0

    def _duplicate_execution_reason(self, event: SignalEvent) -> str:
        if self._has_matching_active_position(event):
            return "position_same_strategy_direction"
        if self._has_matching_pending_entry(event):
            return "pending_entry_same_strategy_direction"
        return ""

    def _has_matching_active_position(self, event: SignalEvent) -> bool:
        if self._position_manager is None:
            return False
        try:
            active_positions = self._position_manager.active_positions()
        except Exception:
            logger.debug(
                "Failed to inspect active positions for duplicate guard",
                exc_info=True,
            )
            return False
        for row in active_positions or []:
            if (
                row.get("symbol") == event.symbol
                and row.get("timeframe") == event.timeframe
                and row.get("strategy") == event.strategy
                and row.get("action") == event.direction
            ):
                return True
        return False

    def _has_matching_pending_entry(self, event: SignalEvent) -> bool:
        if self._pending_manager is None:
            return False
        try:
            status = self._pending_manager.status()
        except Exception:
            logger.debug(
                "Failed to inspect pending entries for duplicate guard",
                exc_info=True,
            )
            return False
        for row in status.get("entries", []) or []:
            if (
                row.get("symbol") == event.symbol
                and row.get("timeframe") == event.timeframe
                and row.get("strategy") == event.strategy
                and row.get("direction") == event.direction
            ):
                return True
        return False

    def _estimate_cost_metrics(
        self,
        event: SignalEvent,
        params: TradeParameters,
    ) -> dict[str, float | None]:
        raw_spread = event.metadata.get("spread_points")
        try:
            spread_points = float(raw_spread) if raw_spread is not None else None
        except (TypeError, ValueError):
            spread_points = None

        raw_symbol_point = event.metadata.get("symbol_point")
        try:
            symbol_point = (
                float(raw_symbol_point) if raw_symbol_point is not None else None
            )
        except (TypeError, ValueError):
            symbol_point = None
        if symbol_point is not None and symbol_point <= 0:
            symbol_point = None

        raw_spread_price = event.metadata.get("spread_price")
        try:
            spread_price = (
                float(raw_spread_price) if raw_spread_price is not None else None
            )
        except (TypeError, ValueError):
            spread_price = None
        if spread_price is None and spread_points is not None and symbol_point is not None:
            spread_price = spread_points * symbol_point
        if spread_points is None and spread_price is not None and symbol_point is not None:
            spread_points = spread_price / symbol_point

        raw_close = event.metadata.get("close_price")
        try:
            close_price = float(raw_close) if raw_close is not None else None
        except (TypeError, ValueError):
            close_price = None
        if close_price is None or close_price <= 0:
            close_price = self._estimate_price(event.indicators)

        stop_distance = None
        reward_distance = None
        if close_price is not None and close_price > 0:
            stop_distance = abs(close_price - params.stop_loss)
            reward_distance = abs(params.take_profit - close_price)

        stop_distance_points = None
        reward_distance_points = None
        if symbol_point is not None and symbol_point > 0:
            if stop_distance is not None:
                stop_distance_points = stop_distance / symbol_point
            if reward_distance is not None:
                reward_distance_points = reward_distance / symbol_point

        spread_to_stop_ratio = None
        if spread_points is not None:
            if stop_distance_points is not None and stop_distance_points > 0:
                spread_to_stop_ratio = round(spread_points / stop_distance_points, 4)
            elif spread_price is not None and stop_distance and stop_distance > 0:
                spread_to_stop_ratio = round(spread_price / stop_distance, 4)

        reward_to_cost_ratio = None
        if spread_points is not None and spread_points > 0:
            if reward_distance_points is not None:
                reward_to_cost_ratio = round(
                    reward_distance_points / spread_points, 4
                )
            elif spread_price is not None and spread_price > 0 and reward_distance is not None:
                reward_to_cost_ratio = round(reward_distance / spread_price, 4)

        return {
            "estimated_cost_points": spread_points,
            "estimated_cost_price": (
                round(spread_price, 6) if spread_price is not None else None
            ),
            "symbol_point": symbol_point,
            "stop_distance": round(stop_distance, 4) if stop_distance is not None else None,
            "stop_distance_points": (
                round(stop_distance_points, 2)
                if stop_distance_points is not None
                else None
            ),
            "reward_distance": round(reward_distance, 4) if reward_distance is not None else None,
            "reward_distance_points": (
                round(reward_distance_points, 2)
                if reward_distance_points is not None
                else None
            ),
            "spread_to_stop_ratio": spread_to_stop_ratio,
            "reward_to_cost_ratio": reward_to_cost_ratio,
        }

    @staticmethod
    def _build_trade_metadata(event: SignalEvent) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "entry_origin": "auto",
            "signal": {
                "signal_id": event.signal_id,
                "strategy": event.strategy,
                "timeframe": event.timeframe,
                "signal_state": event.signal_state,
                "confidence": round(float(event.confidence), 4),
            }
        }
        regime = event.metadata.get("regime")
        if regime is not None:
            metadata["regime"] = regime
        raw_structure = event.metadata.get("market_structure")
        if isinstance(raw_structure, dict):
            metadata["market_structure"] = dict(raw_structure)
        elif hasattr(raw_structure, "to_dict"):
            try:
                metadata["market_structure"] = raw_structure.to_dict()
            except Exception:
                pass
        return metadata

    def _record_slippage(
        self,
        *,
        requested_price: float | None,
        fill_price: float | None,
        symbol_point: float | None,
    ) -> dict[str, float | None]:
        try:
            requested = float(requested_price) if requested_price is not None else None
        except (TypeError, ValueError):
            requested = None
        try:
            filled = float(fill_price) if fill_price is not None else None
        except (TypeError, ValueError):
            filled = None
        if requested is None or filled is None:
            return {
                "requested_price": requested,
                "fill_price": filled,
                "slippage_price": None,
                "slippage_points": None,
            }

        slippage_price = round(filled - requested, 6)
        slippage_points = None
        if symbol_point is not None and symbol_point > 0:
            slippage_points = round(slippage_price / symbol_point, 2)
        self._execution_quality["slippage_samples"] += 1
        self._execution_quality["slippage_total_price"] += slippage_price
        if slippage_points is not None:
            self._execution_quality["slippage_total_points"] += slippage_points
        return {
            "requested_price": requested,
            "fill_price": filled,
            "slippage_price": slippage_price,
            "slippage_points": slippage_points,
        }

    def _execute(
        self,
        event: SignalEvent,
        params: TradeParameters,
        *,
        cost_metrics: dict[str, float | None] | None = None,
    ) -> dict[str, Any | None]:
        payload = {
            "symbol": event.symbol,
            "volume": params.position_size,
            "side": event.direction,
            "order_kind": "market",
            "sl": params.stop_loss,
            "tp": params.take_profit,
            "comment": f"{event.timeframe}:{event.strategy}:{event.direction}"[:31],
            "request_id": event.signal_id,
            "metadata": self._build_trade_metadata(event),
        }

        try:
            result = self._trading.dispatch_operation("trade", payload)
            self._execution_count += 1
            self._last_execution_at = datetime.now(timezone.utc)
            self._last_error = None
            self._last_risk_block = None
            # 执行成功后重置连续失败计数。
            self._consecutive_failures = 0
            requested_price = None
            fill_price = None
            symbol_point = None
            if isinstance(result, dict):
                requested_price = result.get("requested_price") or result.get("price")
                fill_price = result.get("fill_price") or result.get("price")
                try:
                    symbol_point = float(cost_metrics.get("symbol_point")) if cost_metrics else None
                except (TypeError, ValueError):
                    symbol_point = None
                if result.get("recovered_from_state"):
                    self._execution_quality["recovered_from_state"] += 1
            execution_quality = self._record_slippage(
                requested_price=requested_price,
                fill_price=fill_price,
                symbol_point=symbol_point,
            )
            log_entry = {
                "at": self._last_execution_at.isoformat(),
                "signal_id": event.signal_id,
                "symbol": event.symbol,
                "direction": event.direction,
                "strategy": event.strategy,
                "confidence": event.confidence,
                "params": {
                    "volume": params.position_size,
                    "entry_price": fill_price if fill_price is not None else params.entry_price,
                    "sl": params.stop_loss,
                    "tp": params.take_profit,
                    "rr": params.risk_reward_ratio,
                },
                "cost": dict(cost_metrics or {}),
                "execution_quality": execution_quality,
                "success": True,
            }
            self._execution_log.append(log_entry)
            # 记录冷却期 bar_time
            _bar_time = event.metadata.get("bar_time")
            if _bar_time is not None:
                self._last_entry_bar_time[
                    (event.symbol, event.strategy, event.direction)
                ] = _bar_time
            for fn in self._on_trade_executed:
                try:
                    fn(log_entry)
                except Exception:
                    logger.debug("on_trade_executed callback failed", exc_info=True)
            logger.info(
                "TradeExecutor: executed %s %s vol=%.2f sl=%.2f tp=%.2f rr=%.2f (signal=%s)",
                event.direction, event.symbol,
                params.position_size, params.stop_loss, params.take_profit,
                params.risk_reward_ratio, event.signal_id,
            )
            # T-4: 持久化成功执行记录。
            if self._persist_execution_fn is not None:
                try:
                    self._persist_execution_fn([log_entry])
                except Exception as pe:
                    logger.warning("TradeExecutor: persist execution failed: %s", pe)
            if self._position_manager is not None and isinstance(result, dict):
                ticket = result.get("ticket") or result.get("order")
                if ticket:
                    try:
                        self._position_manager.track_position(
                            ticket=int(ticket),
                            signal_id=event.signal_id,
                            symbol=event.symbol,
                            action=event.direction,
                            params=params,
                            timeframe=event.timeframe,
                            strategy=event.strategy,
                            confidence=event.confidence,
                            regime=event.metadata.get("regime"),
                            comment=str(result.get("comment") or payload["comment"]),
                            fill_price=(
                                float(result.get("fill_price"))
                                if result.get("fill_price") is not None
                                else None
                            ),
                        )
                    except Exception as pm_exc:
                        logger.warning(
                            "TradeExecutor: failed to register position ticket=%s: %s",
                            ticket, pm_exc,
                        )
            # 通知 TradeOutcomeTracker 登记新开仓交易。
            if self._trade_outcome_tracker is not None:
                try:
                    self._trade_outcome_tracker.on_trade_opened(
                        signal_id=event.signal_id,
                        symbol=event.symbol,
                        timeframe=event.timeframe,
                        strategy=event.strategy,
                        direction=event.direction,
                        fill_price=(
                            float(result.get("fill_price"))
                            if isinstance(result, dict) and result.get("fill_price") is not None
                            else params.entry_price
                        ),
                        confidence=event.confidence,
                        regime=event.metadata.get("regime"),
                    )
                except Exception as ot_exc:
                    logger.warning(
                        "TradeExecutor: failed to notify trade_outcome_tracker: %s",
                        ot_exc,
                    )
            if isinstance(result, dict):
                result.setdefault("execution_quality", execution_quality)
            return result
        except PreTradeRiskBlockedError as exc:
            self._last_risk_block = str(exc)
            self._execution_quality["risk_blocks"] += 1
            assessment = dict(exc.assessment or {})
            reason = str(assessment.get("reason") or exc)
            self._execution_log.append({
                "at": datetime.now(timezone.utc).isoformat(),
                "signal_id": event.signal_id,
                "symbol": event.symbol,
                "direction": event.direction,
                "strategy": event.strategy,
                "success": False,
                "skipped": True,
                "reason": reason,
                "assessment": assessment,
            })
            self._notify_skip(event.signal_id, reason, event.timeframe or "")
            if self._persist_execution_fn is not None:
                try:
                    self._persist_execution_fn([{
                        "at": datetime.now(timezone.utc).isoformat(),
                        "signal_id": event.signal_id,
                        "symbol": event.symbol,
                        "direction": event.direction,
                        "strategy": event.strategy,
                        "success": False,
                        "error": reason,
                        "metadata": {"blocked_by_risk": assessment},
                    }])
                except Exception as pe:
                    logger.warning("TradeExecutor: persist blocked-entry failed: %s", pe)
            return None
        except Exception as exc:
            self._last_error = str(exc)
            self._consecutive_failures += 1
            self._execution_log.append({
                "at": datetime.now(timezone.utc).isoformat(),
                "signal_id": event.signal_id,
                "symbol": event.symbol,
                "direction": event.direction,
                "strategy": event.strategy,
                "success": False,
                "error": str(exc),
            })
            logger.exception(
                "TradeExecutor: failed to execute %s %s: %s", event.direction, event.symbol, exc,
            )
            # 连续失败达到阈值后打开熔断，避免持续错误重试把交易链路打满。
            if (
                not self._circuit_open
                and self._consecutive_failures >= self.config.max_consecutive_failures
            ):
                self._circuit_open = True
                self._circuit_open_at = datetime.now(timezone.utc)
                logger.error(
                    "TradeExecutor: circuit breaker OPENED after %d consecutive failures. "
                    "Auto-trading suspended. Will auto-reset in %d minutes or call reset_circuit().",
                    self._consecutive_failures,
                    self.config.circuit_auto_reset_minutes,
                )
            # T-4: 持久化失败执行记录。
            fail_entry = {
                "at": datetime.now(timezone.utc).isoformat(),
                "signal_id": event.signal_id,
                "symbol": event.symbol,
                "direction": event.direction,
                "strategy": event.strategy,
                "success": False,
                "error": str(exc),
            }
            if self._persist_execution_fn is not None:
                try:
                    self._persist_execution_fn([fail_entry])
                except Exception as pe:
                    logger.warning("TradeExecutor: persist fail-entry failed: %s", pe)
            return None

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
