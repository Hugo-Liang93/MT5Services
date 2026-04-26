from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Protocol

from ..evaluation.regime import MarketRegimeDetector, RegimeTracker, RegimeType
from ..execution.filters import SignalFilterChain
from ..models import SignalEvent
from ..service import SignalModule, StrategyCapability
from .htf_resolver import parse_htf_config, resolve_htf_indicators
from .policy import RuntimeSignalState, SignalPolicy
from .runtime_components import (
    QueueRunner,
    RuntimeLifecycleManager,
    RuntimePolicyCoordinator,
    RuntimeStatusBuilder,
    SignalLifecyclePolicy,
)
from .runtime_evaluator import (
    apply_confidence_adjustments as _runtime_apply_confidence_adjustments,
)
from .runtime_evaluator import evaluate_strategies as _runtime_evaluate_strategies
from .runtime_evaluator import (
    market_structure_lookback_bars as _runtime_market_structure_lookback_bars,
)
from .runtime_evaluator import publish_signal_event as _runtime_publish_signal_event
from .runtime_evaluator import (
    resolve_market_structure_context as _runtime_resolve_market_structure_context,
)
from .runtime_evaluator import transition_and_publish as _runtime_transition_and_publish
from .runtime_processing import apply_filter_chain as _runtime_apply_filter_chain
from .runtime_processing import detect_regime as _runtime_detect_regime
from .runtime_processing import is_stale_intrabar as _runtime_is_stale_intrabar
from .runtime_processing import process_next_event as _runtime_process_next_event

logger = logging.getLogger(__name__)


class SnapshotSource(Protocol):
    def add_snapshot_listener(
        self,
        listener: Callable[
            [str, str, datetime, dict[str, dict[str, float]], str], None
        ],
    ) -> None: ...

    def get_current_trace_id(self) -> str | None: ...

    def remove_snapshot_listener(
        self,
        listener: Callable[
            [str, str, datetime, dict[str, dict[str, float]], str], None
        ],
    ) -> None: ...

    def get_indicator(
        self, symbol: str, timeframe: str, indicator_name: str
    ) -> dict[str, float] | None: ...
    def get_performance_stats(self) -> dict[str, Any]: ...
    def get_current_spread(self, symbol: str) -> float: ...
    def get_symbol_point(self, symbol: str) -> float | None: ...


@dataclass(frozen=True)
class SignalTarget:
    symbol: str
    timeframe: str
    strategy: str


class SignalRuntime:
    """基于快照事件驱动的信号运行时。"""

    def __init__(
        self,
        service: SignalModule,
        snapshot_source: SnapshotSource,
        targets: Iterable[SignalTarget],
        enable_confirmed_snapshot: bool = True,
        policy: SignalPolicy | None = None,
        filter_chain: SignalFilterChain | None = None,
        regime_detector: MarketRegimeDetector | None = None,
        market_structure_analyzer: Any | None = None,
        htf_indicators_enabled: bool = True,
        intrabar_confidence_factor: float = 1.0,
        htf_context_fn: Callable[..., Any] | None = None,
        htf_target_config: dict[str, str] | None = None,
        warmup_ready_fn: Callable[[], bool] | None = None,
        wal_db_path: str | None = None,
    ):
        self.service = service
        self.snapshot_source = snapshot_source
        self.enable_confirmed_snapshot = bool(enable_confirmed_snapshot)
        self.policy = policy or SignalPolicy()
        self._policy_coordinator = RuntimePolicyCoordinator(self)
        self.filter_chain = filter_chain
        # Regime 检测在 process_next_event 主循环内完成，结果写入 metadata 后再传给 service.evaluate()。
        # 若外部未显式注入 detector，则优先复用 service 的公开端口 regime_detector。
        # 这样每个快照只做一次 regime 判定，避免重复计算。
        self._regime_detector: MarketRegimeDetector = (
            regime_detector or self._resolve_regime_detector(service)
        )
        self._confidence_floor: float = self._resolve_float_value(
            service, "confidence_floor", 0.10
        )
        self._confidence_floor_min_affinity: float = self._resolve_float_value(
            service, "confidence_floor_min_affinity", 0.15
        )
        self._soft_regime_enabled: bool = self._resolve_bool_value(
            service, "soft_regime_enabled", False
        )
        self._has_service_session_reset: bool = hasattr(
            service, "reset_performance_session"
        )
        self._market_structure_analyzer = market_structure_analyzer
        # RegimeTracker 按 (symbol, timeframe) 建立，维护每个交易目标的状态稳定度。
        self._regime_trackers: dict[tuple[str, str], RegimeTracker] = {}
        self._regime_trackers_lock = threading.Lock()
        self._signal_listeners: list[Callable[[SignalEvent], None]] = []
        self._signal_listeners_lock = threading.Lock()
        # Listener 连续失败计数按 id(listener) 统计，达到阈值后自动摘除。
        self._listener_fail_counts: dict[int, int] = {}  # id(listener) -> 连续失败次数
        self._LISTENER_MAX_CONSECUTIVE_FAILURES = 10
        self._targets = list(targets)
        self._target_index: dict[tuple[str, str], list[str]] = {}
        self._apply_policy(self.policy)
        # HTF 指标配置由 SignalModule.htf_target_config() 从策略声明自动推导。
        # 结构为 strategy_name -> {tf: [indicator_names...]}
        self._strategy_htf_config = self._parse_htf_config(htf_target_config or {})
        # 所有 target 中显式声明的 timeframe 集合，供 HTF 解析与运行时约束复用。
        self._configured_timeframes: frozenset[str] = frozenset(
            target.timeframe.upper() for target in self._targets
        )
        # 是否开启 HTF 指标解析与透传。
        self._htf_indicators_enabled: bool = htf_indicators_enabled
        # Intrabar 置信度衰减系数上限为 1.0，避免放大未收盘 bar 的信号强度。
        # 具体策略可再通过 strategy_params 中的 *__intrabar_decay 覆盖默认值。
        self._intrabar_confidence_factor: float = min(intrabar_confidence_factor, 1.0)
        self._strategy_intrabar_decay: dict[str, float] = (
            {}
        )  # populated by set_strategy_intrabar_decay
        # HTF 方向对齐相关参数：冲突惩罚、顺势增益与稳定度加成都会体现在 confidence。
        self._htf_context_fn = htf_context_fn
        # warmup_ready_fn 返回 True 后，运行时才开始接受需要 warmup 的实时评估。
        # 传入 None 表示不做额外 warmup 屏障，standalone/回放场景可直接运行。
        self._warmup_ready_fn: Callable[[], bool] | None = warmup_ready_fn
        # 记录每个 (symbol, tf) 首根 warmup 后的 confirmed bar，作为 intrabar 放行前置条件。
        self._first_realtime_bar_seen: set[tuple[str, str]] = set()
        # 记录每个 (symbol, tf) 首个 intrabar 快照，避免在基础上下文尚未建立时放行。
        self._first_intrabar_snapshot_seen: set[tuple[str, str]] = set()
        # Intrabar 交易协调器：bar 计数稳定性追踪。
        # 由 factories/signals.py 通过 set_intrabar_trade_coordinator() 注入。
        self._intrabar_trade_coordinator: Any | None = None
        # Pipeline tracing（由装配层注入，内部模块通过公开端口读取）。
        self._pipeline_event_bus: Any | None = None
        # 经济日历服务：由装配层通过 set_economic_calendar_service() 显式注入。
        self._economic_calendar_service: Any | None = None
        # 经济事件 decay service：由装配层通过 set_economic_decay_service() 显式注入。
        self._economic_decay_service: Any | None = None

        # R-2: 每个 (symbol, timeframe) 独占一把分片锁，避免同目标状态并发竞争。
        # _state_lock 仅保护全局状态容器；_count_active_states() 等读路径不依赖它持锁遍历。
        self._shard_locks: dict[tuple[str, str], threading.Lock] = {}
        self._meta_lock = threading.Lock()  # 保护 _shard_locks 的懒加载创建。

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._indicator_miss_counts: dict[tuple[str, str, str], int] = {}
        # Separate queues by scope so that intrabar bursts cannot starve
        # confirmed (bar-close) events, which must never be dropped.
        # WAL-backed queue for confirmed events survives process restarts.
        # Intrabar stays in-memory (best-effort, droppable by design).
        self._wal_db_path = wal_db_path
        if wal_db_path:
            from .wal_queue import WalSignalQueue

            self._confirmed_events: Any = WalSignalQueue(wal_db_path)
        else:
            self._confirmed_events: Any = queue.Queue(maxsize=4096)
        self._intrabar_events: queue.Queue = queue.Queue(maxsize=8192)
        self._queue_runner = QueueRunner(self)
        self._status_builder = RuntimeStatusBuilder(self)
        self._lifecycle_policy = SignalLifecyclePolicy(self)
        self._lifecycle_manager = RuntimeLifecycleManager(self)
        self._last_run_at: datetime | None = None
        self._last_error: str | None = None
        self._run_count = 0
        self._processed_events = 0
        self._dropped_events = 0
        self._warmup_skipped = 0
        self._warmup_lock = threading.Lock()  # 保护 warmup 相关共享状态
        # 保护 _state_by_target，避免 background loop 与 status()/API 并发读取时触发字典变更异常。
        # 典型症状是 status() 与后台循环并发时抛出 "dictionary changed size during iteration"。
        self._state_lock = threading.Lock()
        self._dropped_confirmed = 0
        self._dropped_intrabar = 0
        self._intrabar_stale_drops = 0
        self._intrabar_overflow_wait_success = 0
        self._intrabar_overflow_replace_success = 0
        self._intrabar_overflow_failures = 0
        self._confirmed_backpressure_waits = 0
        self._confirmed_backpressure_failures = 0
        self._last_drop_log_at: float = 0.0
        self._dropped_at_last_log: int = (
            0  # 上次日志快照对应的丢弃计数，用于计算本轮 delta。
        )
        self._state_by_target: dict[tuple[str, str, str], RuntimeSignalState] = {}
        # 连续 loop 异常达到阈值后可触发 ERROR/DEGRADED 等运行状态告警。
        # 这里只累计后台主循环故障次数，不把单次策略/监听器异常误判为整体运行故障。
        self._consecutive_loop_errors = 0
        self._loop_error_alert_threshold = 5
        # Per-TF 统计 evaluated / hold / buy_sell / below_min_conf 等评估结果。
        self._per_tf_eval_stats: dict[str, dict[str, int]] = {}
        # FilterChain 按 scope 记录通过/拦截统计。
        self._filter_by_scope: dict[str, dict[str, Any]] = {
            "confirmed": {"passed": 0, "blocked": 0, "blocks": {}},
            "intrabar": {"passed": 0, "blocked": 0, "blocks": {}},
        }
        # 滑动窗口统计最近 1h 内 filter 的 pass/block 分布：(timestamp, scope, category)。
        self._filter_window: deque[tuple[float, str, str]] = deque()
        self._filter_window_seconds: float = 3600.0  # 1 hour
        self._filter_started_at: float = time.monotonic()
        self._htf_stale_counter: list[int] = [0]
        # 经济事件影响预测结果做短 TTL 缓存，避免每个 confirmed 快照都重复外部服务查询。
        self._event_impact_cache: dict[str, Any] = {
            "data": None,
            "expires_at": datetime.now(timezone.utc),
        }
        # Anti-starvation 计数器：限制 confirmed 连续独占队列，周期性给 intrabar 让路。
        self._confirmed_burst_count: int = 0

    @property
    def strategy_capabilities(self) -> dict[str, StrategyCapability]:
        return self.policy.strategy_capabilities

    @property
    def regime_detector(self) -> MarketRegimeDetector:
        """Expose regime detector as a runtime read port."""
        return self._regime_detector

    @property
    def confidence_floor(self) -> float:
        """Expose confidence floor used by downstream confidence adjustments."""
        value = getattr(self.service, "confidence_floor", self._confidence_floor)
        try:
            return float(value)
        except (TypeError, ValueError):
            return self._confidence_floor

    @property
    def confidence_floor_min_affinity(self) -> float:
        """Expose confidence-floor affinity threshold."""
        value = getattr(
            self.service,
            "confidence_floor_min_affinity",
            self._confidence_floor_min_affinity,
        )
        try:
            return float(value)
        except (TypeError, ValueError):
            return self._confidence_floor_min_affinity

    @property
    def soft_regime_enabled(self) -> bool:
        """Expose whether soft regime mode is active."""
        return bool(
            getattr(self.service, "soft_regime_enabled", self._soft_regime_enabled)
        )

    @property
    def economic_calendar_service(self) -> Any | None:
        """Expose explicitly injected economic calendar service."""
        return self._economic_calendar_service

    def set_economic_calendar_service(self, service: Any | None) -> None:
        """Inject economic calendar service for forecast and decay resolution."""
        self._economic_calendar_service = service

    @property
    def economic_decay_service(self) -> Any | None:
        """Expose explicitly injected economic decay service."""
        return self._economic_decay_service

    def set_economic_decay_service(self, service: Any | None) -> None:
        """Inject EconomicDecayService for confidence decay resolution."""
        self._economic_decay_service = service

    def strategy_capability_contract(self) -> tuple[dict[str, Any], ...]:
        """Expose runtime-visible strategy capability contract."""
        return self.policy.strategy_capability_contract()

    def strategy_capability_reconciliation(self) -> dict[str, Any]:
        """Compare module contract vs runtime policy contract.

        Returns a unified diff of any divergences.
        """
        from src.signals import service_diagnostics as _svc_diag

        return _svc_diag.strategy_capability_reconciliation(
            self.service,
            runtime_policy=self.policy,
        )

    def _apply_policy(self, policy: SignalPolicy) -> None:
        self._policy_coordinator.apply(policy)

    def update_policy(self, policy: SignalPolicy) -> None:
        self._apply_policy(policy)

    def start(self) -> None:
        if not self._lifecycle_manager.wait_previous_loop(timeout=5.0):
            return
        self._lifecycle_manager.prepare_startup()
        self._lifecycle_manager.start_loop(self._loop, name="signal-runtime")

    def stop(self, timeout: float = 5.0) -> None:
        self._lifecycle_manager.stop_loop(timeout)
        # WAL queue: don't clear — events persist for restart recovery.
        # In-memory queues: clear as before.
        if not self._wal_db_path:
            self._queue_runner.clear_queues()
        else:
            # Close WAL connection cleanly
            self._confirmed_events.close()
        self._confirmed_burst_count = 0

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------
    # Post-construction injection (public ports for assembly layer)
    # ------------------------------------------------------------------

    def set_warmup_ready_fn(self, fn: Callable[[], bool] | None) -> None:
        self._warmup_ready_fn = fn

    def set_pipeline_event_bus(self, bus: Any | None) -> None:
        self._pipeline_event_bus = bus

    def get_pipeline_event_bus(self) -> Any | None:
        return self._pipeline_event_bus

    @property
    def pipeline_event_bus(self) -> Any | None:
        return self._pipeline_event_bus

    @property
    def market_impact_analyzer(self) -> Any | None:
        service = self.economic_calendar_service
        if service is None:
            return None
        return service.market_impact_analyzer

    def set_intrabar_trade_coordinator(self, coordinator: Any) -> None:
        self._intrabar_trade_coordinator = coordinator

    def _on_snapshot(
        self,
        symbol: str,
        timeframe: str,
        bar_time: datetime,
        indicators: dict[str, dict[str, float]],
        scope: str,
    ) -> None:
        self._queue_runner.on_snapshot(symbol, timeframe, bar_time, indicators, scope)

    def _enqueue(
        self,
        item: tuple[str, str, str, dict[str, dict[str, float]], dict[str, Any]],
    ) -> None:
        self._queue_runner.enqueue(item)

    def _compute_filter_window_stats(self) -> dict:
        return self._status_builder.compute_filter_window_stats()

    def _count_active_states(self) -> dict:
        return self._status_builder.count_active_states()

    def status(self) -> dict:
        return self._status_builder.status()

    def get_regime_stability(
        self, symbol: str, timeframe: str
    ) -> dict[str, Any] | None:
        return self._lifecycle_policy.get_regime_stability(symbol, timeframe)

    def get_regime_stability_map(self) -> dict[str, dict[str, Any]]:
        return self._status_builder.get_regime_stability_map()

    @staticmethod
    def _resolve_regime_detector(service: SignalModule) -> MarketRegimeDetector:
        candidate = getattr(service, "regime_detector", None)
        return candidate if candidate is not None else MarketRegimeDetector()

    @staticmethod
    def _resolve_float_value(service: SignalModule, name: str, default: float) -> float:
        value = getattr(service, name, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _resolve_bool_value(service: SignalModule, name: str, default: bool) -> bool:
        return bool(getattr(service, name, default))

    def set_strategy_intrabar_decay(self, overrides: dict[str, float]) -> None:
        """按策略设置 intrabar 置信度衰减覆盖。"""
        result: dict[str, float] = {}
        for k, v in overrides.items():
            try:
                fv = float(v)
                if fv < 1.0:
                    result[k] = min(fv, 1.0)
            except (TypeError, ValueError):
                logger.warning("Invalid intrabar_decay value for strategy %s: %r", k, v)
        self._strategy_intrabar_decay = result

    def add_signal_listener(self, listener: Callable[[SignalEvent], None]) -> None:
        """注册信号监听器。"""
        with self._signal_listeners_lock:
            if listener not in self._signal_listeners:
                self._signal_listeners.append(listener)

    def remove_signal_listener(self, listener: Callable[[SignalEvent], None]) -> None:
        with self._signal_listeners_lock:
            try:
                self._signal_listeners.remove(listener)
            except ValueError:
                pass
            self._listener_fail_counts.pop(id(listener), None)

    def _publish_signal_event(
        self,
        decision: Any,
        signal_id: str,
        scope: str,
        indicators: dict[str, dict[str, float]],
        transition_metadata: dict[str, Any],
    ) -> None:
        _runtime_publish_signal_event(
            self, decision, signal_id, scope, indicators, transition_metadata
        )

    def _parse_event_time(self, value: Any) -> datetime:
        return SignalLifecyclePolicy.parse_event_time(value)

    def _build_transition_metadata(
        self,
        metadata: dict[str, Any],
        *,
        signal_state: str,
        state_changed: bool,
        previous_state: str,
    ) -> dict[str, Any]:
        return self._lifecycle_policy.build_transition_metadata(
            metadata,
            signal_state=signal_state,
            state_changed=state_changed,
            previous_state=previous_state,
        )

    def _restore_state(self) -> None:
        self._lifecycle_policy.restore_state()

    def _restore_confirmed_state(
        self,
        state: RuntimeSignalState,
        signal_state: str,
        generated_at: datetime,
        bar_time: datetime,
    ) -> None:
        self._lifecycle_policy.restore_confirmed_state(
            state, signal_state, generated_at, bar_time
        )

    def _should_emit(
        self,
        state: RuntimeSignalState,
        signal_state: str,
        event_time: datetime,
        bar_time: datetime,
        *,
        cooldown_seconds: float,
    ) -> bool:
        return self._lifecycle_policy.should_emit(
            state, signal_state, event_time, bar_time, cooldown_seconds=cooldown_seconds
        )

    def _mark_emitted(
        self,
        state: RuntimeSignalState,
        signal_state: str,
        event_time: datetime,
        bar_time: datetime,
    ) -> None:
        self._lifecycle_policy.mark_emitted(state, signal_state, event_time, bar_time)

    @staticmethod
    def _snapshot_signature(indicators: dict[str, dict[str, float]]) -> int:
        return SignalLifecyclePolicy.snapshot_signature(indicators)

    def _is_new_snapshot(
        self,
        state: RuntimeSignalState,
        *,
        scope: str,
        event_time: datetime,
        bar_time: datetime,
        indicators: dict[str, dict[str, float]],
    ) -> bool:
        return self._lifecycle_policy.is_new_snapshot(
            state,
            scope=scope,
            event_time=event_time,
            bar_time=bar_time,
            indicators=indicators,
        )

    def _transition_confirmed(
        self,
        state: RuntimeSignalState,
        decision_action: str,
        event_time: datetime,
        bar_time: datetime,
        metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        return self._lifecycle_policy.transition_confirmed(
            state, decision_action, event_time, bar_time, metadata
        )

    def _all_intrabar_strategies_satisfied(
        self, indicators: dict[str, dict[str, float]]
    ) -> bool:
        return self._lifecycle_policy.all_intrabar_strategies_satisfied(indicators)

    def _get_shard_lock(self, symbol: str, timeframe: str) -> threading.Lock:
        return self._lifecycle_policy.get_shard_lock(symbol, timeframe)

    def _evaluate_strategies(
        self,
        symbol: str,
        timeframe: str,
        scope: str,
        indicators: dict[str, dict[str, float]],
        regime: RegimeType,
        regime_metadata: dict[str, Any],
        event_time: datetime,
        bar_time: datetime,
        active_sessions: list[str],
    ) -> list:
        return _runtime_evaluate_strategies(
            self,
            symbol,
            timeframe,
            scope,
            indicators,
            regime,
            regime_metadata,
            event_time,
            bar_time,
            active_sessions,
        )

    def _apply_confidence_adjustments(
        self,
        decision: Any,
        symbol: str,
        timeframe: str,
        strategy: str,
        scope: str,
        regime_metadata: dict[str, Any],
        event_time: datetime,
    ) -> Any:
        return _runtime_apply_confidence_adjustments(
            self,
            decision,
            symbol,
            timeframe,
            strategy,
            scope,
            regime_metadata,
            event_time,
        )

    def _transition_and_publish(
        self,
        state: "RuntimeSignalState",
        decision: Any,
        scope: str,
        event_time: datetime,
        bar_time: datetime,
        regime_metadata: dict[str, Any],
        scoped_indicators: dict[str, dict[str, float]],
        full_indicators: dict[str, dict[str, float]],
    ) -> None:
        _runtime_transition_and_publish(
            self,
            state,
            decision,
            scope,
            event_time,
            bar_time,
            regime_metadata,
            scoped_indicators,
            full_indicators,
        )

    def _market_structure_lookback_bars(self, timeframe: str) -> int | None:
        return _runtime_market_structure_lookback_bars(self, timeframe)

    def _resolve_market_structure_context(
        self,
        *,
        symbol: str,
        timeframe: str,
        scope: str,
        event_time: datetime,
        bar_time: datetime,
        latest_close: float | None,
    ) -> dict[str, Any]:
        return _runtime_resolve_market_structure_context(
            self,
            symbol=symbol,
            timeframe=timeframe,
            scope=scope,
            event_time=event_time,
            bar_time=bar_time,
            latest_close=latest_close,
        )

    _CONFIRMED_BURST_LIMIT = (
        5  # 连续处理 N 个 confirmed 后，主动让出一次机会给 intrabar。
    )

    def _dequeue_event(self, timeout: float) -> tuple | None:
        return self._queue_runner.dequeue(timeout)

    def _is_stale_intrabar(
        self, scope: str, symbol: str, timeframe: str, metadata: dict[str, Any]
    ) -> bool:
        return _runtime_is_stale_intrabar(scope, symbol, timeframe, metadata)

    def _apply_filter_chain(
        self,
        symbol: str,
        scope: str,
        timeframe: str,
        event_time: datetime,
        indicators: dict[str, dict[str, float]],
        active_sessions: list[str],
        metadata: dict[str, Any],
    ) -> bool:
        return _runtime_apply_filter_chain(
            self,
            symbol,
            scope,
            timeframe,
            event_time,
            indicators,
            active_sessions,
            metadata,
        )

    def _detect_regime(
        self,
        indicators: dict[str, dict[str, float]],
        metadata: dict[str, Any],
        active_sessions: list[str],
    ) -> tuple[RegimeType, dict[str, Any]]:
        return _runtime_detect_regime(self, indicators, metadata, active_sessions)

    def process_next_event(self, timeout: float = 0.5) -> bool:
        return _runtime_process_next_event(self, timeout)

    @staticmethod
    def _parse_htf_config(
        raw: dict[str, str],
    ) -> dict[str, dict[str, list[str]]]:
        return parse_htf_config(raw)

    def _resolve_htf_indicators(
        self,
        symbol: str,
        current_tf: str,
        htf_spec: dict[str, list[str]],
    ) -> dict[str, dict[str, dict[str, Any]]]:
        return resolve_htf_indicators(
            symbol,
            current_tf,
            htf_spec,
            self._configured_timeframes,
            self._get_htf_indicator,
            self._htf_stale_counter,
        )

    def _get_htf_indicator(
        self,
        symbol: str,
        timeframe: str,
        indicator_name: str,
    ) -> dict[str, Any] | None:
        # Subscribe to snapshot_source and wire HTF snapshot listeners.
        return self.snapshot_source.get_indicator(symbol, timeframe, indicator_name)

    def _loop(self) -> None:
        """后台主循环。

        持续调用 process_next_event；若出现异常则进入带上限的退避重试，
        避免忙等刷屏，同时保留恢复机会。
        """
        backoff: float = 0.0
        _session_check_counter = 0
        while not self._stop.is_set():
            try:
                self.process_next_event(timeout=0.5)
                backoff = 0.0  # 成功处理后立即清空异常退避。
                self._consecutive_loop_errors = 0
                # 每 200 次循环做一次轻量维护：session reset 与 miss 计数清理。
                _session_check_counter += 1
                if _session_check_counter >= 200:
                    _session_check_counter = 0
                    if self._has_service_session_reset:
                        self.service.reset_performance_session()
                    # 定期收缩 indicator miss 统计，避免长期运行时键数量无限膨胀。
                    # 这里只保留最常见的 miss key，既保留诊断价值，也控制内存占用。
                    _MAX_MISS_KEYS = 500
                    if len(self._indicator_miss_counts) > _MAX_MISS_KEYS:
                        miss_getter = self._indicator_miss_counts.get
                        sorted_keys = sorted(
                            self._indicator_miss_counts,
                            key=miss_getter,  # type: ignore[arg-type]
                            reverse=True,
                        )
                        # 仅保留 top-N 高频 miss 记录。
                        keep = set(sorted_keys[:_MAX_MISS_KEYS])
                        for k in list(self._indicator_miss_counts):
                            if k not in keep:
                                self._indicator_miss_counts.pop(k, None)
            except Exception as exc:
                self._last_error = str(exc)
                self._consecutive_loop_errors += 1
                logger.exception("Signal runtime event processing failed: %s", exc)
                if self._consecutive_loop_errors >= self._loop_error_alert_threshold:
                    logger.error(
                        "SIGNAL RUNTIME DEGRADED: %d consecutive failures "
                        "(backoff=%.1fs). Signal processing may be stalled. "
                        "Last error: %s",
                        self._consecutive_loop_errors,
                        backoff,
                        exc,
                    )
                backoff = 1.0 if backoff == 0.0 else min(backoff * 2.0, 30.0)
                self._stop.wait(timeout=backoff)
