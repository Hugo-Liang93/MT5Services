from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Iterable, Protocol
from uuid import uuid4

from src.utils.common import timeframe_seconds

from ..evaluation.regime import (
    MarketRegimeDetector,
    RegimeTracker,
    RegimeType,
    SoftRegimeResult,
)
from ..execution.filters import SignalFilterChain
from ..models import SignalEvent
from ..service import SignalModule
from .htf_resolver import parse_htf_config, resolve_htf_indicators
from .policy import RuntimeSignalState, SignalPolicy
from .runtime_evaluator import (
    apply_confidence_adjustments as _runtime_apply_confidence_adjustments,
)
from .runtime_evaluator import evaluate_strategies as _runtime_evaluate_strategies
from .runtime_evaluator import get_vote_emit_kwargs as _runtime_get_vote_emit_kwargs
from .runtime_evaluator import (
    market_structure_lookback_bars as _runtime_market_structure_lookback_bars,
)
from .runtime_evaluator import process_voting as _runtime_process_voting
from .runtime_evaluator import publish_signal_event as _runtime_publish_signal_event
from .runtime_evaluator import (
    resolve_market_structure_context as _runtime_resolve_market_structure_context,
)
from .runtime_evaluator import transition_and_publish as _runtime_transition_and_publish
from .runtime_processing import apply_filter_chain as _runtime_apply_filter_chain
from .runtime_processing import dequeue_event as _runtime_dequeue_event
from .runtime_processing import detect_regime as _runtime_detect_regime
from .runtime_processing import is_stale_intrabar as _runtime_is_stale_intrabar
from .runtime_processing import process_next_event as _runtime_process_next_event
from .runtime_recovery import (
    restore_confirmed_state as _runtime_restore_confirmed_state,
)
from .runtime_recovery import restore_preview_state as _runtime_restore_preview_state
from .runtime_recovery import restore_state as _runtime_restore_state
from .state_machine import build_transition_metadata
from .state_machine import is_new_snapshot as _sm_is_new_snapshot
from .state_machine import mark_emitted as _sm_mark_emitted
from .state_machine import should_emit as _sm_should_emit
from .state_machine import snapshot_signature as _sm_snapshot_signature
from .state_machine import transition_confirmed, transition_intrabar
from .vote_processor import fuse_vote_decisions
from .voting import StrategyVotingEngine

if TYPE_CHECKING:
    from src.indicators.manager import UnifiedIndicatorManager

logger = logging.getLogger(__name__)


class SnapshotSource(Protocol):
    def add_snapshot_listener(
        self,
        listener: Callable[
            [str, str, datetime, dict[str, dict[str, float]], str], None
        ],
    ) -> None: ...

    def remove_snapshot_listener(
        self,
        listener: Callable[
            [str, str, datetime, dict[str, dict[str, float]], str], None
        ],
    ) -> None: ...


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
        enable_intrabar: bool = False,
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
        self.enable_intrabar = bool(enable_intrabar)
        self.policy = policy or SignalPolicy()
        self.filter_chain = filter_chain
        # Regime 检测在 process_next_event 主循环内完成，结果写入 metadata 后再传给 service.evaluate()。
        # 若外部未显式注入 detector，则优先复用 service 上挂载的 detector。
        # 这样每个快照只做一次 regime 判定，避免重复计算。
        self._regime_detector: MarketRegimeDetector = (
            regime_detector
            or getattr(service, "_regime_detector", None)
            or MarketRegimeDetector()
        )
        self._soft_regime_enabled = bool(getattr(service, "soft_regime_enabled", False))
        self._market_structure_analyzer = market_structure_analyzer
        # policy 在未配置 voting_groups 时，使用单层 consensus 投票引擎。
        # voting_groups 会为每个分组创建独立 engine，并在组内汇总策略投票。
        # 一旦启用 voting_groups，全局 consensus engine 就不再参与。
        self._voting_engine: StrategyVotingEngine | None = (
            StrategyVotingEngine(
                consensus_threshold=self.policy.voting_consensus_threshold,
                min_quorum=self.policy.voting_min_quorum,
                disagreement_penalty=self.policy.voting_disagreement_penalty,
            )
            if self.policy.voting_enabled and not self.policy.voting_groups
            else None
        )
        # 分组投票路径维护 (group_config, engine) 对，逐组完成 vote fusion。
        self._voting_group_engines: list[tuple[Any, StrategyVotingEngine]] = (
            self._build_group_engines(self.policy)
        )
        # voting group 成员只参与组内投票，不再作为独立策略单独发信号。
        self._voting_group_members: frozenset[str] = (
            frozenset(
                name for group in self.policy.voting_groups for name in group.strategies
            )
            - self.policy.standalone_override
        )
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
        self._strategy_requirements: dict[str, tuple[str, ...]] = {}
        # 记录每个策略希望接收的 scope 集合。
        # 优先读取 strategy_impl.preferred_scopes，
        # 未声明偏好时默认同时接收 confirmed 与 intrabar。
        self._strategy_scopes: dict[str, frozenset[str]] = {}
        # 策略 regime_affinity 预先缓存，避免在 process_next_event 中重复 getattr/service 调用。
        self._strategy_affinity: dict[str, dict[RegimeType, float]] = {}
        # HTF 指标配置来自 INI [strategy_htf]，解析后供每个策略按需注入高周期指标。
        # 结构为 strategy_name -> {tf: [indicator_names...]}
        self._strategy_htf_config = self._parse_htf_config(htf_target_config or {})
        for target in self._targets:
            self._target_index.setdefault((target.symbol, target.timeframe), []).append(
                target.strategy
            )
            if target.strategy not in self._strategy_requirements:
                self._strategy_requirements[target.strategy] = tuple(
                    self.service.strategy_requirements(target.strategy)
                )
            if target.strategy not in self._strategy_scopes:
                self._strategy_scopes[target.strategy] = frozenset(
                    self.service.strategy_scopes(target.strategy)
                )
            if target.strategy not in self._strategy_affinity:
                self._strategy_affinity[target.strategy] = (
                    self.service.strategy_affinity_map(target.strategy)
                )
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
        # 经济事件影响预测结果做短 TTL 缓存，避免每个 confirmed 快照都重复 getattr/service 查询。
        self._event_impact_cache: dict[str, Any] = {
            "data": None,
            "expires_at": datetime.now(timezone.utc),
        }
        # Anti-starvation 计数器：限制 confirmed 连续独占队列，周期性给 intrabar 让路。
        self._confirmed_burst_count: int = 0
        self._vote_fusion_cache: dict[
            tuple[str, str, datetime], dict[str, tuple[str, Any]]
        ] = {}

    @staticmethod
    def _build_group_engines(
        policy: SignalPolicy,
    ) -> "list[tuple[Any, StrategyVotingEngine]]":
        # Build voting engines from policy.voting_groups.
        return [
            (
                group,
                StrategyVotingEngine(
                    group_name=group.name,
                    consensus_threshold=group.consensus_threshold,
                    min_quorum=group.min_quorum,
                    min_quorum_ratio=group.min_quorum_ratio,
                    disagreement_penalty=group.disagreement_penalty,
                    strategy_weights=group.strategy_weights,
                ),
            )
            for group in policy.voting_groups
        ]

    def update_policy(self, policy: SignalPolicy) -> None:
        self.policy = policy
        self._voting_engine = (
            StrategyVotingEngine(
                consensus_threshold=self.policy.voting_consensus_threshold,
                min_quorum=self.policy.voting_min_quorum,
                disagreement_penalty=self.policy.voting_disagreement_penalty,
            )
            if self.policy.voting_enabled and not self.policy.voting_groups
            else None
        )
        self._voting_group_engines = self._build_group_engines(policy)
        # voting group 成员只参与组内投票，不再作为独立策略单独发信号。
        self._voting_group_members: frozenset[str] = (
            frozenset(
                name for group in self.policy.voting_groups for name in group.strategies
            )
            - self.policy.standalone_override
        )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # WAL queue: close() 后需要 reopen 以重建连接并 reset in-flight 事件
        if self._wal_db_path and hasattr(self._confirmed_events, "reopen"):
            self._confirmed_events.reopen()
        self._restore_state()
        self.snapshot_source.add_snapshot_listener(self._on_snapshot)
        self._thread = threading.Thread(
            target=self._loop, name="signal-runtime", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self.snapshot_source.remove_snapshot_listener(self._on_snapshot)
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None
        # WAL queue: don't clear — events persist for restart recovery.
        # In-memory queues: clear as before.
        if not self._wal_db_path:
            self._clear_queue(self._confirmed_events)
        else:
            # Close WAL connection cleanly
            if hasattr(self._confirmed_events, "close"):
                self._confirmed_events.close()
        self._clear_queue(self._intrabar_events)
        self._confirmed_burst_count = 0

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @staticmethod
    def _clear_queue(q: queue.Queue) -> None:
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                break

    def _on_snapshot(
        self,
        symbol: str,
        timeframe: str,
        bar_time: datetime,
        indicators: dict[str, dict[str, float]],
        scope: str,
    ) -> None:
        if scope == "confirmed" and not self.enable_confirmed_snapshot:
            return
        if scope == "intrabar" and not self.enable_intrabar:
            return
        # Warmup barrier：只有 warmup 完成后，才允许实时 confirmed/intrabar 事件进入评估。
        # warmup 未就绪时直接丢弃实时快照，避免冷启动阶段产生伪信号。
        # 这样可以保证 confirmed 与 intrabar 都建立在已稳定的指标上下文之上。
        # 首次放行以 (symbol, tf) 为粒度，先看到 confirmed bar 再允许对应 intrabar。
        # 防止未收盘快照抢先进入状态机，破坏同一交易目标的事件顺序。
        warmup_ready = (
            self._warmup_ready_fn() if self._warmup_ready_fn is not None else True
        )
        with self._warmup_lock:
            if not warmup_ready:
                self._warmup_skipped += 1
                if self._warmup_skipped <= 5 or self._warmup_skipped % 200 == 0:
                    logger.info(
                        "Warmup barrier (backfilling): %s/%s scope=%s bar_time=%s (total_skipped=%d)",
                        symbol,
                        timeframe,
                        scope,
                        bar_time.isoformat(),
                        self._warmup_skipped,
                    )
                return
            if scope == "confirmed":
                st_key = (symbol, timeframe)
                if st_key not in self._first_realtime_bar_seen:
                    if self._warmup_ready_fn is not None:
                        _bt = (
                            bar_time
                            if bar_time.tzinfo is not None
                            else bar_time.replace(tzinfo=timezone.utc)
                        )
                        tf_secs = timeframe_seconds(timeframe)
                        staleness = (
                            datetime.now(timezone.utc) - _bt.astimezone(timezone.utc)
                        ).total_seconds()
                        max_age = tf_secs * 2 + 30
                        if staleness > max_age:
                            self._warmup_skipped += 1
                            if (
                                self._warmup_skipped <= 10
                                or self._warmup_skipped % 100 == 0
                            ):
                                logger.info(
                                    "Warmup skip (stale bar_time): %s/%s bar_time=%s staleness=%.0fs max_age=%.0fs (total_skipped=%d)",
                                    symbol,
                                    timeframe,
                                    bar_time.isoformat(),
                                    staleness,
                                    max_age,
                                    self._warmup_skipped,
                                )
                            return
                    self._first_realtime_bar_seen.add(st_key)
                    logger.info(
                        "Warmup lifted: first realtime bar for %s/%s at %s",
                        symbol,
                        timeframe,
                        bar_time.isoformat(),
                    )
                required = getattr(
                    self.policy, "warmup_required_indicators", ("atr14",)
                )
                if required and any(ind not in indicators for ind in required):
                    self._warmup_skipped += 1
                    if self._warmup_skipped <= 10 or self._warmup_skipped % 100 == 0:
                        missing = [ind for ind in required if ind not in indicators]
                        logger.info(
                            "Warmup skip (indicators missing: %s): %s/%s (total_skipped=%d)",
                            missing,
                            symbol,
                            timeframe,
                            self._warmup_skipped,
                        )
                    return
            if scope == "intrabar" and self._warmup_ready_fn is not None:
                st_key = (symbol, timeframe)
                if st_key not in self._first_realtime_bar_seen:
                    return
                if st_key not in self._first_intrabar_snapshot_seen:
                    if not self._all_intrabar_strategies_satisfied(indicators):
                        return
                    self._first_intrabar_snapshot_seen.add(st_key)
                    logger.info(
                        "Intrabar ready: first complete snapshot for %s/%s (%d indicators)",
                        symbol,
                        timeframe,
                        len(indicators),
                    )
        # Prefer upstream trace_id from PipelineEventBus (set by bar_event_handler)
        # so the same ID flows through the entire pipeline for frontend tracing.
        upstream_trace_id = getattr(self.snapshot_source, "_current_trace_id", None)
        metadata = {
            "scope": scope,
            "bar_time": bar_time.isoformat(),
            "snapshot_time": datetime.now(timezone.utc).isoformat(),
            "trigger_source": f"{scope}_snapshot",
            "signal_trace_id": upstream_trace_id or uuid4().hex,
        }
        spread_getter = getattr(self.snapshot_source, "get_current_spread", None)
        market_service = getattr(self.snapshot_source, "market_service", None)
        if not callable(spread_getter) and market_service is not None:
            spread_getter = getattr(market_service, "get_current_spread", None)
        point_getter = getattr(self.snapshot_source, "get_symbol_point", None)
        if not callable(point_getter) and market_service is not None:
            point_getter = getattr(market_service, "get_symbol_point", None)
        if callable(spread_getter):
            try:
                spread_points = float(spread_getter(symbol))
                metadata["spread_points"] = spread_points
                if callable(point_getter):
                    point_size = float(point_getter(symbol))
                    metadata["symbol_point"] = point_size
                    metadata["spread_price"] = spread_points * point_size
            except (TypeError, ValueError, AttributeError, KeyError):
                logger.debug(
                    "Failed to resolve spread/point for %s",
                    symbol,
                    exc_info=True,
                )
        if scope == "intrabar":
            if bar_time.tzinfo is None:
                bar_time = bar_time.replace(tzinfo=timezone.utc)
            elapsed = (
                datetime.now(timezone.utc) - bar_time.astimezone(timezone.utc)
            ).total_seconds()
            metadata["bar_progress"] = max(
                0.0, min(elapsed / max(timeframe_seconds(timeframe), 1), 1.0)
            )
        self._enqueue((scope, symbol, timeframe, indicators, metadata))

    def _enqueue(
        self,
        item: tuple[str, str, str, dict[str, dict[str, float]], dict[str, Any]],
    ) -> None:
        # 记录单调时钟入队时间，供后续判断队列等待时长与 stale intrabar。
        item[4]["_enqueued_at"] = time.monotonic()
        scope = item[0]
        target_queue = (
            self._confirmed_events if scope == "confirmed" else self._intrabar_events
        )
        try:
            target_queue.put_nowait(item)
        except queue.Full:
            if scope == "confirmed":
                try:
                    # confirmed 事件允许一次带超时的回压等待，尽量避免关键信号被瞬时挤掉。
                    self._confirmed_backpressure_waits += 1
                    target_queue.put(
                        item,
                        timeout=max(
                            self.policy.confirmed_queue_backpressure_timeout_seconds,
                            0.0,
                        ),
                    )
                    return
                except queue.Full:
                    self._confirmed_backpressure_failures += 1
            self._dropped_events += 1
            if scope == "confirmed":
                self._dropped_confirmed += 1
                # confirmed 事件最终仍被丢弃时提升到 WARNING，便于排查实时链路拥塞。
                symbol, timeframe = item[1], item[2]
                logger.warning(
                    "CONFIRMED event DROPPED for %s/%s (backpressure_failures=%d, "
                    "total_confirmed_dropped=%d). Bar-close signal permanently lost!",
                    symbol,
                    timeframe,
                    self._confirmed_backpressure_failures,
                    self._dropped_confirmed,
                )
            else:
                self._dropped_intrabar += 1
            now = time.monotonic()
            # Rate-limit error logs to at most once per 60 s to avoid log spam.
            if now - self._last_drop_log_at >= 60.0:
                delta = self._dropped_events - self._dropped_at_last_log
                self._last_drop_log_at = now
                self._dropped_at_last_log = self._dropped_events
                symbol, timeframe = item[1], item[2]
                logger.error(
                    "Signal runtime %s queue is full - indicator snapshot dropped "
                    "(dropped_since_last_log=%d, total_dropped=%d, "
                    "scope=%s, symbol=%s, timeframe=%s, maxsize=%d). "
                    "Consider increasing queue capacity or reducing event frequency.",
                    scope,
                    delta,
                    self._dropped_events,
                    scope,
                    symbol,
                    timeframe,
                    target_queue.maxsize,
                )

    def _compute_filter_window_stats(self) -> dict:
        # Compute rolling 1h filter pass/block stats grouped by scope.
        now = time.monotonic()
        cutoff = now - self._filter_window_seconds
        while self._filter_window and self._filter_window[0][0] < cutoff:
            self._filter_window.popleft()

        by_scope: dict[str, dict[str, Any]] = {}
        for _, scope, cat in self._filter_window:
            if scope not in by_scope:
                by_scope[scope] = {"passed": 0, "blocked": 0, "blocks": {}}
            s = by_scope[scope]
            if cat == "_pass":
                s["passed"] += 1
            else:
                s["blocked"] += 1
                s["blocks"][cat] = s["blocks"].get(cat, 0) + 1

        elapsed = now - self._filter_started_at
        window_secs = min(elapsed, self._filter_window_seconds)

        return {
            "filter_window_seconds": self._filter_window_seconds,
            "filter_window_elapsed": round(window_secs, 0),
            "filter_window_by_scope": by_scope,
        }

    def _count_active_states(self) -> dict:
        # Restore preview and confirmed state from recent persisted rows.
        with self._state_lock:
            snapshot = list(self._state_by_target.values())
        return {
            "active_preview_states": sum(
                1 for s in snapshot if s.preview_state != "idle"
            ),
            "active_confirmed_states": sum(
                1 for s in snapshot if s.confirmed_state != "idle"
            ),
        }

    def status(self) -> dict:
        market_structure_enabled = False
        if self._market_structure_analyzer is not None:
            market_structure_enabled = bool(
                getattr(
                    getattr(self._market_structure_analyzer, "config", None),
                    "enabled",
                    True,
                )
            )
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "target_count": len(self._targets),
            "trigger_mode": {
                "confirmed_snapshot": self.enable_confirmed_snapshot,
                "intrabar": self.enable_intrabar,
            },
            "strategy_sessions": {
                name: list(sessions)
                for name, sessions in self.policy.strategy_sessions.items()
            },
            "market_structure_enabled": market_structure_enabled,
            "market_structure_cache_entries": (
                self._market_structure_analyzer.cache_entries
                if self._market_structure_analyzer is not None
                else 0
            ),
            "strategy_scopes": {
                name: sorted(scopes) for name, scopes in self._strategy_scopes.items()
            },
            "run_count": self._run_count,
            "processed_events": self._processed_events,
            "dropped_events": self._dropped_events,
            "dropped_confirmed": self._dropped_confirmed,
            "dropped_intrabar": self._dropped_intrabar,
            "confirmed_backpressure_waits": self._confirmed_backpressure_waits,
            "confirmed_backpressure_failures": self._confirmed_backpressure_failures,
            "confirmed_queue_size": self._confirmed_events.qsize(),
            "confirmed_queue_capacity": self._confirmed_events.maxsize,
            "intrabar_queue_size": self._intrabar_events.qsize(),
            "intrabar_queue_capacity": self._intrabar_events.maxsize,
            "last_run_at": self._last_run_at.isoformat() if self._last_run_at else None,
            "last_error": self._last_error,
            "filter_by_scope": {
                s: {
                    "passed": d["passed"],
                    "blocked": d["blocked"],
                    "blocks": dict(d["blocks"]),
                }
                for s, d in self._filter_by_scope.items()
            },
            **self._compute_filter_window_stats(),
            "htf_stale_warnings": self._htf_stale_counter[0],
            "warmup_skipped": self._warmup_skipped,  # atomic read under GIL
            "warmup_ready": (
                self._warmup_ready_fn() if self._warmup_ready_fn is not None else True
            ),
            "warmup_realtime_symbols": len(
                self._first_realtime_bar_seen
            ),  # atomic read under GIL
            # 即使 background loop 异常退出，这里也继续暴露当前活跃状态计数。
            **self._count_active_states(),
            "voting_groups": self._voting_groups_summary(),
            "regime_map": self.get_regime_stability_map(),
            "per_tf_eval_stats": dict(self._per_tf_eval_stats),
            "filter_realtime_status": (
                self.filter_chain.filter_status()
                if self.filter_chain is not None
                and hasattr(self.filter_chain, "filter_status")
                else {}
            ),
        }

    def _voting_groups_summary(self) -> list[dict[str, Any]]:
        """返回当前配置的 voting group 摘要。"""
        result: list[dict[str, Any]] = []
        for group_config, _engine in self._voting_group_engines:
            result.append(
                {
                    "name": group_config.name,
                    "strategies": sorted(group_config.strategies),
                }
            )
        return result

    def get_regime_stability(
        self, symbol: str, timeframe: str
    ) -> dict[str, Any] | None:
        with self._regime_trackers_lock:
            tracker = self._regime_trackers.get((symbol, timeframe))
        return tracker.describe() if tracker else None

    def get_regime_stability_map(self) -> dict[str, dict[str, Any]]:
        with self._regime_trackers_lock:
            snapshot = list(self._regime_trackers.items())
        return {f"{sym}/{tf}": tracker.describe() for (sym, tf), tracker in snapshot}

    def get_voting_info(self) -> dict[str, Any]:
        voting_engine = self._voting_engine
        return {
            "voting_enabled": voting_engine is not None,
            "voting_config": voting_engine.describe() if voting_engine else None,
        }

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

    @staticmethod
    def _parse_event_time(value: Any) -> datetime:
        if isinstance(value, datetime):
            return (
                value
                if value.tzinfo is not None
                else value.replace(tzinfo=timezone.utc)
            )
        text = str(value)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return (
            parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        )

    @staticmethod
    def _build_transition_metadata(
        metadata: dict[str, Any],
        *,
        signal_state: str,
        state_changed: bool,
        previous_state: str,
    ) -> dict[str, Any]:
        return build_transition_metadata(
            metadata,
            signal_state=signal_state,
            state_changed=state_changed,
            previous_state=previous_state,
        )

    def _restore_state(self) -> None:
        _runtime_restore_state(self)

    def _restore_confirmed_state(
        self,
        state: RuntimeSignalState,
        signal_state: str,
        generated_at: datetime,
        bar_time: datetime,
    ) -> None:
        _runtime_restore_confirmed_state(state, signal_state, generated_at, bar_time)

    def _restore_preview_state(
        self,
        timeframe: str,
        state: RuntimeSignalState,
        signal_state: str,
        generated_at: datetime,
        bar_time: datetime,
        now: datetime,
    ) -> None:
        _runtime_restore_preview_state(
            timeframe, state, signal_state, generated_at, bar_time, now
        )

    @staticmethod
    def _should_emit(
        state: RuntimeSignalState,
        signal_state: str,
        event_time: datetime,
        bar_time: datetime,
        *,
        cooldown_seconds: float,
    ) -> bool:
        return _sm_should_emit(
            state, signal_state, event_time, bar_time, cooldown_seconds=cooldown_seconds
        )

    @staticmethod
    def _mark_emitted(
        state: RuntimeSignalState,
        signal_state: str,
        event_time: datetime,
        bar_time: datetime,
    ) -> None:
        _sm_mark_emitted(state, signal_state, event_time, bar_time)

    @staticmethod
    def _snapshot_signature(indicators: dict[str, dict[str, float]]) -> int:
        return _sm_snapshot_signature(indicators)

    def _is_new_snapshot(
        self,
        state: RuntimeSignalState,
        *,
        scope: str,
        event_time: datetime,
        bar_time: datetime,
        indicators: dict[str, dict[str, float]],
    ) -> bool:
        return _sm_is_new_snapshot(
            state,
            scope=scope,
            event_time=event_time,
            bar_time=bar_time,
            indicators=indicators,
            dedupe_window_seconds=self.policy.snapshot_dedupe_window_seconds,
        )

    @staticmethod
    def _transition_confirmed(
        state: RuntimeSignalState,
        decision_action: str,
        event_time: datetime,
        bar_time: datetime,
        metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        return transition_confirmed(
            state, decision_action, event_time, bar_time, metadata
        )

    def _transition_intrabar(
        self,
        state: RuntimeSignalState,
        decision_action: str,
        confidence: float,
        event_time: datetime,
        bar_time: datetime,
        metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        return transition_intrabar(
            state,
            decision_action,
            confidence,
            event_time,
            bar_time,
            metadata,
            min_preview_confidence=self.policy.min_preview_confidence,
            min_preview_bar_progress=self.policy.min_preview_bar_progress,
            min_preview_stable_seconds=self.policy.min_preview_stable_seconds,
            preview_cooldown_seconds=self.policy.preview_cooldown_seconds,
        )

    def _all_intrabar_strategies_satisfied(
        self, indicators: dict[str, dict[str, float]]
    ) -> bool:
        """检查所有 intrabar 策略所需指标是否已就绪。

        warmup 阶段只有在 intrabar pipeline 已具备全部 required_indicators 时才允许继续，
        这样可以避免策略在缺失关键指标的首批快照上被错误评估。
        若任一 intrabar 策略仍缺指标，则返回 False，由上游继续等待。
        """
        if not indicators:
            return False
        indicator_names = set(indicators.keys())
        for strategy, scopes in self._strategy_scopes.items():
            if "intrabar" not in scopes:
                continue
            required = self._strategy_requirements.get(strategy, ())
            if required and any(ind not in indicator_names for ind in required):
                return False
        return True

    def _get_shard_lock(self, symbol: str, timeframe: str) -> threading.Lock:
        """按 (symbol, timeframe) 获取分片锁。

        不能直接用 dict.setdefault 在无锁路径创建 Lock，否则同一 key 可能被并发初始化多次。
        这里用 meta_lock 包住懒加载，形成轻量的 double-checked locking。
        """
        key = (symbol, timeframe)
        lock = self._shard_locks.get(key)
        if lock is not None:
            return lock
        with self._meta_lock:
            return self._shard_locks.setdefault(key, threading.Lock())

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
    ) -> Any:
        return _runtime_apply_confidence_adjustments(
            self, decision, symbol, timeframe, strategy, scope, regime_metadata
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

    def _get_vote_emit_kwargs(self) -> dict:
        return _runtime_get_vote_emit_kwargs(self)

    def _process_voting(
        self,
        snapshot_decisions: list,
        symbol: str,
        timeframe: str,
        scope: str,
        regime: RegimeType,
        regime_stability: float,
        regime_metadata: dict[str, Any],
        indicators: dict[str, dict[str, float]],
        event_time: datetime,
        bar_time: datetime,
    ) -> None:
        _runtime_process_voting(
            self,
            snapshot_decisions,
            symbol,
            timeframe,
            scope,
            regime,
            regime_stability,
            regime_metadata,
            indicators,
            event_time,
            bar_time,
        )

    _CONFIRMED_BURST_LIMIT = (
        5  # 连续处理 N 个 confirmed 后，主动让出一次机会给 intrabar。
    )

    def _dequeue_event(self, timeout: float) -> tuple | None:
        return _runtime_dequeue_event(self, timeout)

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
        getter = getattr(self.snapshot_source, "get_indicator", None)
        if callable(getter):
            return getter(symbol, timeframe, indicator_name)
        return None

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
                    perf_tracker = getattr(self.service, "_performance_tracker", None)
                    if perf_tracker is not None:
                        perf_tracker.check_session_reset()
                    # 定期收缩 indicator miss 统计，避免长期运行时键数量无限膨胀。
                    # 这里只保留最常见的 miss key，既保留诊断价值，也控制内存占用。
                    _MAX_MISS_KEYS = 500
                    if len(self._indicator_miss_counts) > _MAX_MISS_KEYS:
                        sorted_keys = sorted(
                            self._indicator_miss_counts,
                            key=self._indicator_miss_counts.get,  # type: ignore[arg-type]
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
