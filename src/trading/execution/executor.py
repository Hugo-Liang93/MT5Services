"""TradeExecutor：消费 execution-intent 或本地适配事件并执行自动下单。

职责边界保持清晰：
- 信号模块只负责生成并发布 `SignalEvent`
- live/runtime 正式主链由 `ExecutionIntentConsumer -> TradeExecutor.process_event()`
- `on_signal_event()` / `on_intrabar_trade_signal()` 仅保留为本地适配入口
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
from src.signals.contracts import StrategyDeployment
from src.signals.metadata_keys import MetadataKey as MK
from src.signals.models import SignalEvent

from ..pending.manager import PendingEntryManager
from ..ports import ExecutorTradingPort
from ..positions.manager import PositionManager
from ..runtime.lifecycle import OwnedThreadLifecycle
from ..tracking.trade_outcome import TradeOutcomeTracker
from .eventing import emit_execution_blocked as _emit_execution_blocked_helper
from .eventing import emit_execution_decided as _emit_execution_decided_helper
from .eventing import emit_admission_report as _emit_admission_report_helper
from .eventing import emit_blocked_admission_report as _emit_blocked_admission_report_helper
from .eventing import emit_terminal_execution_event as _emit_terminal_execution_event_helper
from .eventing import notify_skip as _notify_skip_helper
from .decision_engine import ExecutionDecisionEngine
from .pre_trade_pipeline import PreTradePipeline
from .gate import ExecutionGate, ExecutionGateConfig
from .params import estimate_price as _estimate_price_helper
from .params import get_account_balance as _get_account_balance_helper
from .result_recorder import ExecutionResultRecorder
from . import pre_trade_checks as _ptc
from .risk_caps import MaxSingleTradeLossPolicy
from .sizing import RegimeSizing, extract_atr_from_indicators
from .reasons import (
    REASON_AUTO_TRADE_DISABLED,
    REASON_INTRABAR_GUARD_MISSING,
    REASON_INTRABAR_POSITION_ALREADY_VALIDATED,
    REASON_INTRABAR_POSITION_HOLD,
    REASON_INTRABAR_PARENT_BAR_MISSING,
    REASON_INTRABAR_GUARD_BLOCKED,
    REASON_INTRABAR_GATE_BLOCKED,
    REASON_SPREAD_TO_STOP_RATIO_TOO_HIGH,
    REASON_TRADE_PARAMS_UNAVAILABLE,
    SKIP_CATEGORY_TRADE_PARAMS,
    SKIP_CATEGORY_COST_GUARD,
    SKIP_CATEGORY_DISPATCH,
    reason_category,
)

logger = logging.getLogger(__name__)


@dataclass
class ExecutorConfig:
    enabled: bool = False
    min_confidence: float = 0.7
    # 按时间框架覆盖最低交易置信度，低周期可以设置更严格阈值。
    timeframe_min_confidence: dict[str, float] = field(default_factory=dict)
    # HTF 冲突阻止规则的适用时间框架集合。
    htf_conflict_block_timeframes: frozenset[str] = field(default_factory=frozenset)
    # 豁免 HTF 冲突阻止的策略类别集合。
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
    exec_queue_size: int = 256
    strategy_deployments: dict[str, StrategyDeployment] = field(default_factory=dict)
    # 单笔交易绝对亏损 hard cap（USD），None = 不启用（需调用方显式声明）
    max_single_trade_loss_policy: MaxSingleTradeLossPolicy = field(
        default_factory=lambda: MaxSingleTradeLossPolicy(max_loss_usd=None)
    )


class TradeExecutor:
    """Subscribes to SignalRuntime events and auto-executes confirmed trades.

    Execution is **non-blocking**: ``on_signal_event()`` enqueues the event
    and returns immediately so that SignalRuntime's main loop is never stalled
    by slow MT5 API calls.  A background daemon thread drains the queue.
    """

    def __init__(
        self,
        *,
        trading_module: ExecutorTradingPort,
        runtime_identity: Any,
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
        quote_health_fn: Callable[[str | None], dict[str, Any]] | None = None,
        circuit_breaker_history_fn: Callable[[list], None] | None = None,
    ):
        # §0dj：runtime_identity 必填。环境维度的 deployment 门禁、execution
        # intent owner、trace 上下文都依赖它；没有 identity 不允许装配执行器。
        if runtime_identity is None:
            raise ValueError(
                "TradeExecutor requires runtime_identity (RuntimeIdentity); "
                "executor 装配契约依赖 environment / instance_id / account_key 等"
            )
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
        self._quote_health_fn = quote_health_fn
        self._pipeline_event_bus = pipeline_event_bus
        self._runtime_identity = runtime_identity
        self._circuit_breaker_history_fn = circuit_breaker_history_fn
        self._execution_count = 0
        self._last_execution_at: datetime | None = None
        self._last_error: str | None = None
        self._last_risk_block: str | None = None
        self._execution_log: deque[dict] = deque(maxlen=100)
        self._pre_trade_pipeline = PreTradePipeline(self)
        self._decision_engine = ExecutionDecisionEngine(self)
        self._result_recorder = ExecutionResultRecorder(self)
        # Async execution: decouple listener callback from MT5 API calls
        exec_queue_size = int(self.config.exec_queue_size)
        self._exec_queue: queue.Queue = queue.Queue(maxsize=exec_queue_size)
        self._exec_thread: threading.Thread | None = None
        self._exec_lifecycle = OwnedThreadLifecycle(
            self, "_exec_thread", label="TradeExecutor"
        )
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
        self._intrabar_guard: Any | None = None  # set via set_intrabar_guard()
        # Intrabar 执行统计
        self._intrabar_signals_received: int = 0
        self._intrabar_signals_executed: int = 0

    @property
    def pipeline_event_bus(self) -> PipelineEventBus | None:
        return self._pipeline_event_bus

    def get_pipeline_event_bus(self) -> PipelineEventBus | None:
        return self._pipeline_event_bus

    def set_pipeline_event_bus(self, bus: PipelineEventBus | None) -> None:
        self._pipeline_event_bus = bus

    @property
    def trading(self):
        return self._trading

    @property
    def account_balance_getter(self) -> Any | None:
        return self._account_balance_getter

    @property
    def position_manager(self):
        return self._position_manager

    @property
    def persist_execution_fn(self) -> Callable[[list], None] | None:
        return self._persist_execution_fn

    @property
    def on_execution_skip(self) -> Callable[[str, str], None] | None:
        return self._on_execution_skip

    @property
    def on_trade_executed(self) -> list[Callable[[dict], None]]:
        return self._on_trade_executed

    @property
    def execution_gate(self) -> "ExecutionGate":
        return self._execution_gate

    @property
    def pending_manager(self) -> PendingEntryManager | None:
        return self._pending_manager

    @property
    def performance_tracker(self):
        return self._performance_tracker

    @property
    def equity_curve_filter(self):
        return self._equity_curve_filter

    @property
    def quote_health_fn(self) -> Callable[[str | None], dict[str, Any]] | None:
        return self._quote_health_fn

    @property
    def runtime_identity(self):
        return self._runtime_identity

    @property
    def execution_quality(self) -> dict[str, float | int]:
        return self._execution_quality

    @property
    def dropped_signals(self) -> list[dict[str, Any]]:
        return self._dropped_signals

    @dropped_signals.setter
    def dropped_signals(self, value: list[dict[str, Any]]) -> None:
        self._dropped_signals = value

    @property
    def max_dropped_history(self) -> int:
        return self._max_dropped_history

    @property
    def intrabar_guard(self):
        return self._intrabar_guard

    @property
    def margin_guard(self):
        return self._margin_guard

    @property
    def skip_lock(self):
        return self._skip_lock

    @property
    def skip_reasons(self):
        return self._skip_reasons

    @property
    def tf_stats(self):
        return self._tf_stats

    @property
    def execution_count(self) -> int:
        return self._execution_count

    @execution_count.setter
    def execution_count(self, value: int) -> None:
        self._execution_count = value

    @property
    def last_execution_at(self) -> datetime | None:
        return self._last_execution_at

    @last_execution_at.setter
    def last_execution_at(self, value: datetime | None) -> None:
        self._last_execution_at = value

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @last_error.setter
    def last_error(self, value: str | None) -> None:
        self._last_error = value

    @property
    def last_risk_block(self) -> str | None:
        return self._last_risk_block

    @last_risk_block.setter
    def last_risk_block(self, value: str | None) -> None:
        self._last_risk_block = value

    @property
    def execution_log(self) -> deque[dict]:
        return self._execution_log

    @property
    def trade_outcome_tracker(self) -> TradeOutcomeTracker | None:
        return self._trade_outcome_tracker

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @consecutive_failures.setter
    def consecutive_failures(self, value: int) -> None:
        self._consecutive_failures = value

    @property
    def circuit_open(self) -> bool:
        return self._circuit_open

    @circuit_open.setter
    def circuit_open(self, value: bool) -> None:
        self._circuit_open = value

    @property
    def circuit_open_at(self) -> datetime | None:
        return self._circuit_open_at

    @circuit_open_at.setter
    def circuit_open_at(self, value: datetime | None) -> None:
        self._circuit_open_at = value

    @property
    def health_check_failures(self) -> int:
        return self._health_check_failures

    @health_check_failures.setter
    def health_check_failures(self, value: int) -> None:
        self._health_check_failures = value

    @property
    def max_health_check_failures(self) -> int:
        return self._MAX_HEALTH_CHECK_FAILURES

    @property
    def last_entry_bar_time(self) -> dict[tuple[str, str, str], datetime]:
        return self._last_entry_bar_time

    @property
    def executed_signal_ids(self) -> deque[str]:
        return self._executed_signal_ids

    def quote_health(self, symbol: str | None) -> dict[str, Any]:
        if self._quote_health_fn is None:
            return {
                "stale": False,
                "age_seconds": None,
                "stale_threshold_seconds": None,
            }
        try:
            snapshot = self._quote_health_fn(symbol)
        except Exception:
            logger.debug(
                "TradeExecutor: quote health probe failed for %s",
                symbol,
                exc_info=True,
            )
            return {
                "stale": True,
                "age_seconds": None,
                "stale_threshold_seconds": None,
            }
        if not isinstance(snapshot, dict):
            return {
                "stale": False,
                "age_seconds": None,
                "stale_threshold_seconds": None,
            }
        return {
            "stale": bool(snapshot.get("stale", False)),
            "age_seconds": snapshot.get("age_seconds"),
            "stale_threshold_seconds": snapshot.get("stale_threshold_seconds"),
        }

    # ------------------------------------------------------------------
    # Public listener interface
    # ------------------------------------------------------------------

    def on_signal_event(self, event: SignalEvent) -> None:
        """本地适配入口：把 confirmed 事件送入执行队列。

        正式 live 主链通常由 ExecutionIntentConsumer 直接调用 process_event()。
        这里保留为单进程/测试场景的非阻塞适配层。
        """
        if event.scope != "confirmed":
            return
        if "confirmed" not in event.signal_state:
            return
        # 没有 signal_id 的 confirmed 事件不进入执行链。
        if not event.signal_id:
            return
        # Ensure worker thread is running (lazy start)
        if not self._exec_lifecycle.is_running():
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
                logger.error(
                    "TradeExecutor queue full after 3s retry, DROPPING confirmed "
                    "event %s/%s/%s signal_id=%s (overflows=%d). Trading "
                    "opportunity permanently lost!",
                    event.symbol,
                    event.timeframe,
                    event.strategy,
                    event.signal_id,
                    self._execution_quality["queue_overflows"] + 1,
                )
                self._result_recorder.record_overflow(event)

    def on_intrabar_trade_signal(self, event: SignalEvent) -> None:
        """本地适配入口：把 intrabar_armed_* 事件送入执行队列。

        正式 live 主链通常由 ExecutionIntentConsumer 直接调用 process_event()。
        这里保留为单进程/测试场景的 best-effort 适配层。
        """
        if event.scope != "intrabar":
            return
        if not event.signal_state.startswith("intrabar_armed_"):
            return
        if not event.signal_id:
            return
        self._intrabar_signals_received += 1
        if not self._exec_lifecycle.is_running():
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
        if self._exec_lifecycle.is_running():
            return
        self._stop_event.clear()
        self._exec_lifecycle.ensure_running(
            lambda: threading.Thread(
                target=self._exec_worker, name="trade-executor", daemon=True
            )
        )

    def start(self) -> None:
        """启用执行器，允许后续 confirmed 事件重新拉起执行线程。"""
        # 等待上一次 stop() 超时遗留的僵尸线程退出
        if not self._exec_lifecycle.wait_previous():
            logger.error(
                "TradeExecutor: previous worker still alive after re-join, "
                "new signals will reuse it instead of creating a new thread"
            )
        self._stop_event.clear()

    def is_running(self) -> bool:
        return self._exec_lifecycle.is_running()

    def set_intrabar_guard(self, guard: Any) -> None:
        """注入 IntrabarTradeGuard（供运行时初始化入口使用）。"""
        self._intrabar_guard = guard

    def update_execution_gate_config(self, config: Any) -> None:
        """热更新 ExecutionGate 配置。"""
        self._execution_gate.config = config

    def _exec_worker(self) -> None:
        """后台消费执行队列，串行处理 confirmed 和 intrabar 交易事件。"""
        while not self._stop_event.is_set() or not self._exec_queue.empty():
            try:
                event = self._exec_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                result = self.process_event(event)
                _emit_terminal_execution_event_helper(
                    pipeline_event_bus=self.get_pipeline_event_bus(),
                    event=event,
                    result=result,
                    extra_payload=self._build_local_terminal_payload(event),
                )
            except Exception as exc:
                logger.error("TradeExecutor worker error", exc_info=True)
                _emit_terminal_execution_event_helper(
                    pipeline_event_bus=self.get_pipeline_event_bus(),
                    event=event,
                    result={
                        "status": "failed",
                        "reason": str(exc),
                        "category": SKIP_CATEGORY_DISPATCH,
                        "error_code": type(exc).__name__,
                        "details": {"error": str(exc)},
                    },
                    extra_payload=self._build_local_terminal_payload(event),
                )
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
        self._exec_lifecycle.stop(self._stop_event, timeout=timeout)
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

    def process_event(self, event: SignalEvent) -> dict[str, Any] | None:
        if event.scope == "intrabar":
            return self._handle_intrabar_entry(event)
        return self._handle_confirmed(event)

    def record_circuit_breaker_event(
        self,
        *,
        event: str,
        breaker_type: str = "executor",
        reason: str | None = None,
        consecutive_failures: int | None = None,
        account_alias: str | None = None,
        account_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._circuit_breaker_history_fn is None:
            return
        # §0dj：runtime_identity 必填，无 None 检查 + getattr 兜底。
        identity = self._runtime_identity
        payload = dict(metadata or {})
        payload.setdefault("instance_id", identity.instance_id)
        payload.setdefault("instance_role", identity.instance_role)
        self._circuit_breaker_history_fn(
            [
                (
                    datetime.now(timezone.utc),
                    account_alias or identity.account_alias,
                    account_key or identity.account_key,
                    breaker_type,
                    event,
                    (
                        self._consecutive_failures
                        if consecutive_failures is None
                        else int(consecutive_failures)
                    ),
                    reason,
                    payload,
                )
            ]
        )

    def reset_circuit(
        self,
        *,
        event: str = "reset",
        reason: str | None = "manual_reset",
    ) -> None:
        """Reset the circuit breaker manually."""
        previous_failures = self._consecutive_failures
        self._circuit_open = False
        self._consecutive_failures = 0
        self._circuit_open_at = None
        self._health_check_failures = 0
        self.record_circuit_breaker_event(
            event=event,
            reason=reason,
            consecutive_failures=previous_failures,
        )
        logger.info("TradeExecutor: circuit breaker manually reset")

    def _check_trading_health(self) -> bool:
        return _ptc.check_trading_health(self)

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
        _ptc.reject_signal(
            self, event, reason, category, tf,
            log_level=log_level, extra_log=extra_log,
            pipeline_reason=pipeline_reason,
        )

    def _check_circuit_breaker(self, event: SignalEvent) -> bool:
        return _ptc.check_circuit_breaker(self, event)

    def _check_reentry_cooldown(self, event: SignalEvent, tf: str) -> bool:
        return _ptc.check_reentry_cooldown(self, event, tf)

    def _run_pre_trade_filters(self, event: SignalEvent, tf: str) -> str | None:
        return self._pre_trade_pipeline.run_confirmed(event, tf)

    def _build_local_terminal_payload(self, event: SignalEvent) -> dict[str, Any]:
        # §0dj：runtime_identity 必填，无防御性 None 检查 + getattr 兜底。
        metadata = dict(event.metadata or {})
        payload: dict[str, Any] = {
            "account_key": self._runtime_identity.account_key,
            "account_alias": self._runtime_identity.account_alias,
            "instance_id": self._runtime_identity.instance_id,
            "instance_role": self._runtime_identity.instance_role,
        }
        for key in (
            "intent_id",
            "intent_key",
            "target_account_key",
            "target_account_alias",
            "action_id",
        ):
            value = metadata.get(key)
            if value not in (None, ""):
                payload[key] = value
        return {key: value for key, value in payload.items() if value not in (None, "")}

    def _build_skipped_result(
        self,
        reason: str,
        *,
        category: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_reason = str(reason or "skipped").strip() or "skipped"
        normalized_category = str(
            category or reason_category(normalized_reason)
        ).strip() or reason_category(normalized_reason)
        result: dict[str, Any] = {
            "status": "skipped",
            "reason": normalized_reason,
            "skip_reason": normalized_reason,
            "category": normalized_category,
            "skip_category": normalized_category,
        }
        if details:
            result["details"] = dict(details)
        return result

    def _record_skip_result(
        self,
        event: SignalEvent,
        reason: str,
        tf: str,
        *,
        category: str | None = None,
        details: dict[str, Any] | None = None,
        log_level: str = "info",
        message: str | None = None,
        set_last_risk_block: bool = False,
    ) -> dict[str, Any]:
        normalized_reason = str(reason or "skipped").strip() or "skipped"
        normalized_category = str(
            category or reason_category(normalized_reason)
        ).strip() or reason_category(normalized_reason)
        if set_last_risk_block:
            self.last_risk_block = normalized_reason
        log_message = message or (
            f"TradeExecutor: skipping {event.symbol}/{event.strategy} "
            f"{event.direction} - {normalized_reason}"
        )
        if log_level == "error":
            logger.error(log_message)
        elif log_level == "warning":
            logger.warning(log_message)
        elif log_level == "debug":
            logger.debug(log_message)
        else:
            logger.info(log_message)
        self.execution_log.append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "signal_id": event.signal_id,
                "symbol": event.symbol,
                "direction": event.direction,
                "strategy": event.strategy,
                "success": False,
                "skipped": True,
                "reason": normalized_reason,
            }
        )
        _notify_skip_helper(self, event.signal_id, normalized_reason, tf)
        return self._build_skipped_result(
            normalized_reason,
            category=normalized_category,
            details=details,
        )

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
                    return self._record_skip_result(
                        event,
                        REASON_INTRABAR_POSITION_ALREADY_VALIDATED,
                        tf,
                        details={
                            "intrabar_direction": intra_dir,
                            "bar_time": (
                                bar_time.isoformat()
                                if isinstance(bar_time, datetime)
                                else bar_time
                            ),
                        },
                        message=(
                            "Intrabar position validated by confirmed signal: "
                            f"{event.symbol}/{event.timeframe}/{event.strategy} {intra_dir}"
                        ),
                    )
                elif event.direction not in ("buy", "sell"):
                    # hold → 不动，交给出场规则
                    return self._record_skip_result(
                        event,
                        REASON_INTRABAR_POSITION_HOLD,
                        tf,
                        details={
                            "intrabar_direction": intra_dir,
                            "bar_time": (
                                bar_time.isoformat()
                                if isinstance(bar_time, datetime)
                                else bar_time
                            ),
                        },
                        message=(
                            "Confirmed hold with intrabar position, keeping: "
                            f"{event.symbol}/{event.timeframe}/{event.strategy}"
                        ),
                    )
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
            return self._record_skip_result(
                event,
                REASON_AUTO_TRADE_DISABLED,
                tf,
                set_last_risk_block=True,
            )

        # ── 预交易过滤链 ──
        reject_reason = self._pre_trade_pipeline.run_confirmed(event, tf)
        if reject_reason is not None:
            return self._build_skipped_result(
                reject_reason,
                category=reason_category(reject_reason),
            )

        # ── 交易参数计算 ──
        decision = self._decision_engine.build_confirmed_decision(event)
        if decision.trade_params is None:
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
                decision.reject_reason or REASON_TRADE_PARAMS_UNAVAILABLE,
                SKIP_CATEGORY_TRADE_PARAMS,
                tf,
            )
            return self._build_skipped_result(
                decision.reject_reason or REASON_TRADE_PARAMS_UNAVAILABLE,
                category=SKIP_CATEGORY_TRADE_PARAMS,
                details={
                    "atr": atr,
                    "balance": balance,
                    "close_price": close_price,
                },
            )

        # ── Spread-to-stop 比率检查 ──
        cost_metrics = decision.cost_metrics or {}
        spread_to_stop_ratio = cost_metrics.get("spread_to_stop_ratio")
        if (
            spread_to_stop_ratio is not None
            and spread_to_stop_ratio > self.config.max_spread_to_stop_ratio
        ):
            self._reject_signal(
                event,
                REASON_SPREAD_TO_STOP_RATIO_TOO_HIGH,
                SKIP_CATEGORY_COST_GUARD,
                tf,
                extra_log=f"{spread_to_stop_ratio:.3f} > {self.config.max_spread_to_stop_ratio:.3f}",
            )
            return self._build_skipped_result(
                REASON_SPREAD_TO_STOP_RATIO_TOO_HIGH,
                category=SKIP_CATEGORY_COST_GUARD,
                details=dict(cost_metrics),
            )

        # ── 全部通过，派发执行 ──
        _emit_admission_report_helper(
            self,
            event,
            decision="allow",
            stage="account_risk",
            requested_operation="signal_execution",
            reasons=[
                {
                    "code": "checks_passed",
                    "stage": "account_risk",
                    "message": "confirmed pre-trade checks passed",
                    "details": {
                        "timeframe": tf,
                        "strategy": event.strategy,
                    },
                }
            ],
        )
        # §0dl P2：旧实现在 _decision_engine.execute() 之前就把 sig_id 追加进
        # _executed_signal_ids → 一次 dispatch 失败/异常会让该 signal 永远被
        # duplicate_signal_id 拒，与 execution-intent 的 lease/retry 语义冲突。
        # 修复：execute() 完成后才 append（执行已实际派发到 broker，重复检测
        # 才有意义）；execute 抛异常时 append 不发生，让 lease 自然 retry。
        sig_id = event.signal_id or ""
        self._signals_passed += 1
        if tf:
            with self._skip_lock:
                self._tf_stats.setdefault(
                    tf, {"received": 0, "passed": 0, "skip_reasons": {}}
                )["passed"] += 1
        entry_spec = event.metadata.get(MK.ENTRY_SPEC, {})
        entry_type = entry_spec.get("entry_type", decision.entry_type or "market")
        use_market = decision.use_market or self._pending_manager is None

        _emit_execution_decided_helper(
            self,
            event,
            order_kind="market" if use_market else entry_type,
        )
        result = self._decision_engine.execute(event, decision)
        # execute() 已派发（result 已构造，broker dispatch 已 atomic 完成）→
        # 真正"执行过"才记入 dedup 列表。失败语义继续由 result.status 表达，
        # 不污染 dedup 状态。
        if sig_id:
            self._executed_signal_ids.append(sig_id)
        return result

    # ------------------------------------------------------------------
    # Intrabar 交易执行
    # ------------------------------------------------------------------

    def _handle_intrabar_entry(self, event: SignalEvent) -> dict[str, Any] | None:
        """Intrabar armed 信号执行。复用完整 pre-trade filter chain。"""
        tf = event.timeframe or ""
        if not self.config.enabled:
            return self._record_skip_result(
                event,
                REASON_AUTO_TRADE_DISABLED,
                tf,
                set_last_risk_block=True,
            )
        intrabar_pre_trade = self._pre_trade_pipeline.run_intrabar(event, tf)
        if not intrabar_pre_trade.can_execute:
            reject_reason = intrabar_pre_trade.reject_reason
            if reject_reason == REASON_INTRABAR_GUARD_MISSING:
                logger.warning(
                    "TradeExecutor: intrabar guard not injected, "
                    "dropping intrabar signal %s/%s/%s",
                    event.symbol,
                    event.timeframe,
                    event.strategy,
                )
            elif reject_reason == REASON_INTRABAR_PARENT_BAR_MISSING:
                logger.warning("TradeExecutor: no parent_bar_time in intrabar event, skip")
            elif reject_reason == REASON_INTRABAR_GUARD_BLOCKED:
                logger.debug(
                    "IntrabarTradeGuard blocked: %s/%s/%s reason=%s",
                    event.symbol,
                    event.timeframe,
                    event.strategy,
                    intrabar_pre_trade.detail,
                )
            elif reject_reason == REASON_INTRABAR_GATE_BLOCKED:
                logger.debug(
                    "ExecutionGate.check_intrabar blocked: %s/%s/%s reason=%s",
                    event.symbol,
                    event.timeframe,
                    event.strategy,
                    intrabar_pre_trade.detail,
                )
            else:
                logger.debug(
                    "Intrabar pre-trade blocked: %s/%s/%s reason=%s detail=%s",
                    event.symbol,
                    event.timeframe,
                    event.strategy,
                    reject_reason,
                    intrabar_pre_trade.detail,
                )
            blocked_category = (
                "execution_gate"
                if reject_reason in {
                    REASON_INTRABAR_GUARD_MISSING,
                    REASON_INTRABAR_PARENT_BAR_MISSING,
                    REASON_INTRABAR_GUARD_BLOCKED,
                    REASON_INTRABAR_GATE_BLOCKED,
                }
                else reason_category(str(reject_reason or ""))
            )
            blocked_details = {
                "parent_bar_time": (
                    parent_bar_time.isoformat()
                    if isinstance(
                        (parent_bar_time := intrabar_pre_trade.parent_bar_time),
                        datetime,
                    )
                    else parent_bar_time
                ),
                "reject_reason": reject_reason,
            }
            if reject_reason in {
                REASON_INTRABAR_GUARD_MISSING,
                REASON_INTRABAR_PARENT_BAR_MISSING,
                REASON_INTRABAR_GUARD_BLOCKED,
                REASON_INTRABAR_GATE_BLOCKED,
            }:
                _emit_blocked_admission_report_helper(
                    self,
                    event,
                    code=reject_reason or "intrabar_blocked",
                    category=blocked_category,
                    message=str(
                        intrabar_pre_trade.detail
                        or reject_reason
                        or "intrabar blocked"
                    ),
                    details=blocked_details,
                    requested_operation="intrabar_execution",
                )
            return self._build_skipped_result(
                reject_reason or "intrabar_blocked",
                category=blocked_category,
                details=blocked_details,
            )
        parent_bar_time = intrabar_pre_trade.parent_bar_time

        # 4. 交易参数计算（ATR 用上一根确认 bar 的）
        decision = self._decision_engine.build_intrabar_decision(event)
        if decision.trade_params is None:
            logger.warning(
                "TradeExecutor: cannot compute params for intrabar entry " "%s/%s/%s",
                event.symbol,
                event.timeframe,
                event.strategy,
            )
            _emit_blocked_admission_report_helper(
                self,
                event,
                code=decision.reject_reason or REASON_TRADE_PARAMS_UNAVAILABLE,
                category=SKIP_CATEGORY_TRADE_PARAMS,
                message="intrabar trade parameters unavailable",
                details={
                    "parent_bar_time": (
                        parent_bar_time.isoformat()
                        if isinstance(parent_bar_time, datetime)
                        else parent_bar_time
                    ),
                },
                requested_operation="intrabar_execution",
            )
            return self._build_skipped_result(
                decision.reject_reason or REASON_TRADE_PARAMS_UNAVAILABLE,
                category=SKIP_CATEGORY_TRADE_PARAMS,
                details={
                    "parent_bar_time": (
                        parent_bar_time.isoformat()
                        if isinstance(parent_bar_time, datetime)
                        else parent_bar_time
                    ),
                },
            )

        # 5. 成本检查
        cost_metrics = decision.cost_metrics
        if cost_metrics is not None and cost_metrics.get("blocked"):
            reject_reason = str(
                cost_metrics.get("reason")
                or REASON_SPREAD_TO_STOP_RATIO_TOO_HIGH
            ).strip() or REASON_SPREAD_TO_STOP_RATIO_TOO_HIGH
            logger.info(
                "Intrabar entry blocked by cost check: %s/%s/%s",
                event.symbol,
                event.timeframe,
                event.strategy,
            )
            _emit_blocked_admission_report_helper(
                self,
                event,
                code=reject_reason,
                category=SKIP_CATEGORY_COST_GUARD,
                message="intrabar entry blocked by cost check",
                details=dict(cost_metrics),
                requested_operation="intrabar_execution",
            )
            return self._build_skipped_result(
                reject_reason,
                category=SKIP_CATEGORY_COST_GUARD,
                details=dict(cost_metrics),
            )

        # 6. 执行（复用现有 market/pending 逻辑）
        _emit_admission_report_helper(
            self,
            event,
            decision="allow",
            stage="account_risk",
            requested_operation="intrabar_execution",
            reasons=[
                {
                    "code": "checks_passed",
                    "stage": "account_risk",
                    "message": "intrabar pre-trade checks passed",
                    "details": {
                        "parent_bar_time": (
                            parent_bar_time.isoformat() if isinstance(parent_bar_time, datetime) else parent_bar_time
                        ),
                    },
                }
            ],
        )
        result = self._decision_engine.execute(event, decision)
        if result is None:
            return self._build_skipped_result(
                "execution_result_unavailable",
                category=SKIP_CATEGORY_DISPATCH,
            )

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
        return result

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
                "trade_trigger_strategies": [],
                "intrabar_trading_enabled": bool(
                    self._execution_gate.config.intrabar_trading_enabled
                ),
                "intrabar_enabled_strategies": sorted(
                    self._execution_gate.config.intrabar_enabled_strategies
                ),
            },
            "strategy_deployments": {
                name: deployment.to_dict()
                for name, deployment in self.config.strategy_deployments.items()
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
