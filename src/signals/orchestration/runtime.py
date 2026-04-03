from __future__ import annotations

import dataclasses as _dc
import logging
import queue
from collections import deque
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Iterable,
    Protocol,
)
from uuid import uuid4

from src.utils.common import timeframe_seconds

from ..confidence import apply_intrabar_decay
from ..evaluation.indicators_helpers import extract_close_price
from ..evaluation.regime import (
    MarketRegimeDetector,
    RegimeTracker,
    RegimeType,
    SoftRegimeResult,
)
from ..execution.filters import SignalFilterChain
from ..models import SignalEvent
from ..service import SignalModule
from .affinity import (
    any_strategy_eligible as _any_strategy_eligible_fn,
    effective_affinity as _effective_affinity_fn,
)
from .htf_resolver import (
    compute_htf_alignment,
    parse_htf_config,
    resolve_htf_indicators,
)
from .policy import RuntimeSignalState, SignalPolicy
from .vote_processor import (
    fuse_vote_decisions,
    process_voting as _do_process_voting,
)
from .state_machine import (
    build_transition_metadata,
    mark_emitted as _sm_mark_emitted,
    should_emit as _sm_should_emit,
    is_new_snapshot as _sm_is_new_snapshot,
    snapshot_signature as _sm_snapshot_signature,
    transition_confirmed,
    transition_intrabar,
)
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
        htf_direction_fn: Callable[[str, str], str | None] | None = None,
        htf_context_fn: Callable[..., Any] | None = None,
        htf_conflict_penalty: float = 0.70,
        htf_alignment_boost: float = 1.10,
        htf_alignment_strength_coefficient: float = 0.30,
        htf_alignment_stability_per_bar: float = 0.03,
        htf_alignment_stability_cap: float = 1.15,
        htf_alignment_intrabar_strength_ratio: float = 0.50,
        htf_target_config: dict[str, str] | None = None,
        warmup_ready_fn: Callable[[], bool] | None = None,
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
        self._voting_group_members: frozenset[str] = frozenset(
            name
            for group in self.policy.voting_groups
            for name in group.strategies
        ) - self.policy.standalone_override
        # RegimeTracker 按 (symbol, timeframe) 建立，维护每个交易目标的状态稳定度。
        self._regime_trackers: dict[tuple[str, str], RegimeTracker] = {}
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
        self._htf_direction_fn = htf_direction_fn
        self._htf_context_fn = htf_context_fn
        self._htf_conflict_penalty: float = htf_conflict_penalty
        self._htf_alignment_boost: float = htf_alignment_boost
        self._htf_strength_coeff: float = htf_alignment_strength_coefficient
        self._htf_stability_per_bar: float = htf_alignment_stability_per_bar
        self._htf_stability_cap: float = htf_alignment_stability_cap
        self._htf_intrabar_ratio: float = htf_alignment_intrabar_strength_ratio
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
        # Confirmed snapshots can briefly burst during startup catch-up after
        # ingestion backfills closed bars. Keep enough headroom to buffer that
        # replay without dropping durable bar-close signals.
        self._confirmed_events: queue.Queue = queue.Queue(maxsize=4096)
        self._intrabar_events: queue.Queue = queue.Queue(maxsize=8192)
        self._last_run_at: datetime | None = None
        self._last_error: str | None = None
        self._run_count = 0
        self._processed_events = 0
        self._dropped_events = 0
        self._warmup_skipped = 0
        # 保护 _state_by_target，避免 background loop 与 status()/API 并发读取时触发字典变更异常。
        # 典型症状是 status() 与后台循环并发时抛出 "dictionary changed size during iteration"。
        self._state_lock = threading.Lock()
        self._dropped_confirmed = 0
        self._dropped_intrabar = 0
        self._confirmed_backpressure_waits = 0
        self._confirmed_backpressure_failures = 0
        self._last_drop_log_at: float = 0.0
        self._dropped_at_last_log: int = 0  # 上次日志快照对应的丢弃计数，用于计算本轮 delta。
        self._state_by_target: dict[tuple[str, str, str], RuntimeSignalState] = {}
        # 连续 loop 异常达到阈值后可触发 ERROR/DEGRADED 等运行状态告警。
        # 这里只累计后台主循环故障次数，不把单次策略/监听器异常误判为整体运行故障。
        self._consecutive_loop_errors = 0
        self._loop_error_alert_threshold = 5
        self._affinity_gates_skipped: int = 0
        # Per-TF 统计被 affinity/raw_conf 等原因跳过的次数。
        self._per_tf_skips: dict[str, dict[str, int]] = {}
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
        # legacy 字段已删除，相关缓存现由 MarketStructureAnalyzer 内部维护。

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
        self._voting_group_members: frozenset[str] = frozenset(
            name
            for group in self.policy.voting_groups
            for name in group.strategies
        ) - self.policy.standalone_override

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
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
        self._clear_queue(self._confirmed_events)
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
                # Validate bar_time is truly recent before treating this as
                # the first realtime bar.  BackgroundIngestor sets
                # _backfill_done when enqueueing completes, but indicator/
                # snapshot processing is async; stale backfill snapshots
                # may still arrive after the flag flips.  A genuine realtime
                # confirmed bar closes at most 1 bar-duration ago (+ margin
                # for processing delay).
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
                    # Allow up to 2x timeframe duration + 30s processing margin
                    max_age = tf_secs * 2 + 30
                    if staleness > max_age:
                        self._warmup_skipped += 1
                        if self._warmup_skipped <= 10 or self._warmup_skipped % 100 == 0:
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
            # 指标就绪性检查：交易执行至少需要 ATR。
            # 如果快照里还没有 atr14，说明指标热身尚未完成，
            # 此时跳过可以避免把首个 state_changed=true 机会浪费在
            # 实际不可执行的信号上。
            required = getattr(self.policy, "warmup_required_indicators", ("atr14",))
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
                # 没看到首根 confirmed bar 之前，不放行任何 intrabar。
                return
            # 首个 intrabar 快照到来时先登记，确保 pipeline 已建立对应目标的基础上下文。
            # 如果当前 intrabar 缺少首轮 required_indicators，则继续等待而不是告警。
            # 这样可以避免 warmup 切换边界上，残缺 intrabar 快照误触发 required_indicators 校验。
            if st_key not in self._first_intrabar_snapshot_seen:
                if not self._all_intrabar_strategies_satisfied(indicators):
                    return
                self._first_intrabar_snapshot_seen.add(st_key)
                logger.info(
                    "Intrabar ready: first complete snapshot for %s/%s (%d indicators)",
                    symbol, timeframe, len(indicators),
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
                    "Failed to resolve spread/point for %s", symbol, exc_info=True,
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
            # Aggregate fields kept for backward compatibility.
            "queue_size": self._confirmed_events.qsize()
            + self._intrabar_events.qsize(),
            "queue_capacity": self._confirmed_events.maxsize
            + self._intrabar_events.maxsize,
            "last_run_at": self._last_run_at.isoformat() if self._last_run_at else None,
            "last_error": self._last_error,
            "affinity_gates_skipped": self._affinity_gates_skipped,
            "per_tf_skips": dict(self._per_tf_skips),
            "filter_by_scope": {
                s: {"passed": d["passed"], "blocked": d["blocked"], "blocks": dict(d["blocks"])}
                for s, d in self._filter_by_scope.items()
            },
            **self._compute_filter_window_stats(),
            "htf_stale_warnings": self._htf_stale_counter[0],
            "warmup_skipped": self._warmup_skipped,
            "warmup_ready": (
                self._warmup_ready_fn() if self._warmup_ready_fn is not None else True
            ),
            "warmup_realtime_symbols": len(self._first_realtime_bar_seen),
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
            result.append({
                "name": group_config.name,
                "strategies": sorted(group_config.strategies),
            })
        return result

    def get_regime_stability(
        self, symbol: str, timeframe: str
    ) -> dict[str, Any] | None:
        tracker = self._regime_trackers.get((symbol, timeframe))
        return tracker.describe() if tracker else None

    def get_regime_stability_map(self) -> dict[str, dict[str, Any]]:
        return {
            f"{sym}/{tf}": tracker.describe()
            for (sym, tf), tracker in self._regime_trackers.items()
        }

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

    def _publish_signal_event(
        self,
        decision: Any,
        signal_id: str,
        scope: str,
        indicators: dict[str, dict[str, float]],
        transition_metadata: dict[str, Any],
    ) -> None:
        with self._signal_listeners_lock:
            listeners = list(self._signal_listeners)
        if not listeners:
            return
        signal_state = transition_metadata.get("signal_state", "")
        event = SignalEvent(
            symbol=decision.symbol,
            timeframe=decision.timeframe,
            strategy=decision.strategy,
            direction=decision.direction,
            confidence=decision.confidence,
            signal_state=signal_state,
            scope=scope,
            indicators=indicators,
            metadata=transition_metadata,
            generated_at=decision.timestamp,
            signal_id=signal_id,
            reason=decision.reason,
        )
        # Pipeline trace: broadcast signal_evaluated event
        pipeline_bus = getattr(self, "_pipeline_event_bus", None)
        trace_id = transition_metadata.get("signal_trace_id")
        if pipeline_bus is not None and trace_id:
            pipeline_bus.emit_signal_evaluated(
                trace_id=trace_id,
                symbol=decision.symbol,
                timeframe=decision.timeframe,
                scope=scope,
                strategy=decision.strategy,
                direction=decision.direction,
                confidence=decision.confidence,
                signal_state=signal_state,
            )
        listeners_to_remove: list[Callable] = []
        for listener in listeners:
            lid = id(listener)
            try:
                listener(event)
                # 成功回调后清空该 listener 的连续失败计数。
                self._listener_fail_counts.pop(lid, None)
            except Exception as exc:
                fail_count = self._listener_fail_counts.get(lid, 0) + 1
                self._listener_fail_counts[lid] = fail_count
                listener_name = getattr(listener, "__name__", repr(listener))
                error_msg = f"Signal listener error [{listener_name}]: {exc} (failures={fail_count})"
                self._last_error = error_msg
                logger.error(error_msg, exc_info=True)
                if fail_count >= self._LISTENER_MAX_CONSECUTIVE_FAILURES:
                    logger.error(
                        "LISTENER CIRCUIT BREAK: %s reached %d consecutive failures, "
                        "auto-deregistering to prevent cascading errors",
                        listener_name,
                        fail_count,
                    )
                    listeners_to_remove.append(listener)
        if listeners_to_remove:
            with self._signal_listeners_lock:
                for listener in listeners_to_remove:
                    try:
                        self._signal_listeners.remove(listener)
                    except ValueError:
                        pass
                    self._listener_fail_counts.pop(id(listener), None)

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
        recent_signals = getattr(self.service, "recent_signals", None)
        if not callable(recent_signals):
            return

        limit = max(len(self._targets) * 6, 200)
        try:
            rows = recent_signals(scope="all", limit=limit)
        except Exception:
            logger.exception("Failed to restore signal runtime state from repository")
            return

        now = datetime.now(timezone.utc)
        restored_confirmed: set[tuple[str, str, str]] = set()
        restored_preview: set[tuple[str, str, str]] = set()
        # Track the timestamp of each restored confirmed state so that
        # preview rows for the same key that pre-date the confirmed event
        # are not mistakenly restored on top of it.
        restored_confirmed_at: dict[tuple[str, str, str], datetime] = {}
        for row in rows:
            key = (row.get("symbol"), row.get("timeframe"), row.get("strategy"))
            if key[0] is None or key[1] is None or key[2] is None:
                continue
            metadata = row.get("metadata") or {}
            signal_state = str(metadata.get("signal_state", "")).strip().lower()
            scope = (
                str(row.get("scope") or metadata.get("scope") or "confirmed")
                .strip()
                .lower()
            )
            generated_at_raw = row.get("generated_at") or metadata.get("snapshot_time")
            bar_time_raw = metadata.get("bar_time") or row.get("generated_at")
            if generated_at_raw is None or bar_time_raw is None:
                continue
            generated_at = self._parse_event_time(generated_at_raw)
            bar_time = self._parse_event_time(bar_time_raw)
            state = self._state_by_target.setdefault(key, RuntimeSignalState())

            if scope == "confirmed" and key not in restored_confirmed:
                restored_confirmed.add(key)
                restored_confirmed_at[key] = generated_at
                self._restore_confirmed_state(
                    state, signal_state, generated_at, bar_time
                )
                continue

            if scope in {"preview", "intrabar"} and key not in restored_preview:
                # Don't restore a preview state that pre-dates an already-
                # restored confirmed event: the confirmed state supersedes it.
                confirmed_at = restored_confirmed_at.get(key)
                if confirmed_at is not None and generated_at <= confirmed_at:
                    continue
                restored_preview.add(key)
                self._restore_preview_state(
                    key[1], state, signal_state, generated_at, bar_time, now
                )

    def _restore_confirmed_state(
        self,
        state: RuntimeSignalState,
        signal_state: str,
        generated_at: datetime,
        bar_time: datetime,
    ) -> None:
        if signal_state == "confirmed_cancelled":
            state.confirmed_state = "idle"
            state.confirmed_bar_time = bar_time
            state.last_emitted_state = signal_state
            state.last_emitted_at = generated_at
            state.last_emitted_bar_time = bar_time
            return
        if signal_state not in {"confirmed_buy", "confirmed_sell"}:
            return
        state.confirmed_state = signal_state
        state.confirmed_bar_time = bar_time
        state.last_emitted_state = signal_state
        state.last_emitted_at = generated_at
        state.last_emitted_bar_time = bar_time

    def _restore_preview_state(
        self,
        timeframe: str,
        state: RuntimeSignalState,
        signal_state: str,
        generated_at: datetime,
        bar_time: datetime,
        now: datetime,
    ) -> None:
        if signal_state not in {
            "preview_buy",
            "preview_sell",
            "armed_buy",
            "armed_sell",
        }:
            return
        if now >= bar_time + timedelta(seconds=max(timeframe_seconds(timeframe), 1)):
            return
        action = signal_state.rsplit("_", 1)[-1]
        state.preview_state = signal_state
        state.preview_action = action
        state.preview_bar_time = bar_time
        state.preview_since = generated_at
        state.last_emitted_state = signal_state
        state.last_emitted_at = generated_at
        state.last_emitted_bar_time = bar_time

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
        """评估单个 snapshot 下所有可运行策略，并返回 SignalDecision 列表。

        流程包括：
        1. 按 scope / regime / affinity 做预筛选
        2. 裁剪 scoped_indicators
        3. 校验 snapshot signature 是否为新快照
        4. 调用 service.evaluate()
        5. 驱动 transition_confirmed / transition_intrabar
        6. 汇总结果并更新运行时统计
        """
        snapshot_decisions: list = []
        min_affinity_skip = self.policy.min_affinity_skip
        # 市场结构上下文按 snapshot 只解析一次，随后复用到同批所有策略的 metadata。
        _structure_resolved = False
        strategies = self._target_index.get((symbol, timeframe), [])
        shard_lock = self._get_shard_lock(symbol, timeframe)

        # Pre-parse soft regime once for all strategies in this snapshot
        _soft_parsed: SoftRegimeResult | None = None
        if self._soft_regime_enabled and regime_metadata.get("_soft_regime"):
            try:
                _soft_parsed = SoftRegimeResult.from_dict(
                    regime_metadata["_soft_regime"]
                )
            except Exception:
                logger.info(
                    "Failed to parse soft regime for %s/%s, falling back to hard regime",
                    symbol,
                    timeframe,
                    exc_info=True,
                )

        # event impact forecast 使用 5 分钟 TTL 缓存，避免每个 confirmed bar 都重复查服务/DB。
        _event_impact: dict[str, Any] | None = None
        _impact_analyzer = getattr(self, "_market_impact_analyzer", None)
        if _impact_analyzer is not None and scope == "confirmed":
            cache = self._event_impact_cache
            if event_time >= cache.get("expires_at", event_time):
                try:
                    eco_service = getattr(self, "_economic_calendar_service", None)
                    if eco_service is not None:
                        upcoming_events = eco_service.get_high_impact_events(
                            hours=2, limit=1
                        )
                        if upcoming_events:
                            cache["data"] = _impact_analyzer.get_impact_forecast(
                                upcoming_events[0].event_name, symbol=symbol
                            )
                        else:
                            cache["data"] = None
                    cache["expires_at"] = event_time + timedelta(minutes=5)
                except Exception:
                    cache["data"] = None
                    cache["expires_at"] = event_time + timedelta(minutes=5)
            _event_impact = cache.get("data")

        for strategy in strategies:
            allowed_sessions = self.policy.strategy_sessions.get(strategy, ())
            if allowed_sessions and not any(
                session_name in allowed_sessions for session_name in active_sessions
            ):
                continue

            allowed_timeframes = self.policy.strategy_timeframes.get(strategy, ())
            if allowed_timeframes and timeframe not in allowed_timeframes:
                continue

            allowed_scopes = self._strategy_scopes.get(
                strategy, frozenset(("intrabar", "confirmed"))
            )
            if scope not in allowed_scopes:
                continue

            required_indicators = self._strategy_requirements.get(strategy, ())
            if required_indicators:
                missing = [ind for ind in required_indicators if ind not in indicators]
                if missing:
                    # 缺指标计数按 (symbol, tf, strategy) 聚合，仅在首次与每 100 次时打印告警。
                    miss_key = (symbol, timeframe, strategy)
                    self._indicator_miss_counts[miss_key] = (
                        self._indicator_miss_counts.get(miss_key, 0) + 1
                    )
                    # miss key 过多时只保留 top-200 高频项，避免计数表无限增长。
                    if len(self._indicator_miss_counts) > 600:
                        _keep_top = 200
                        _sorted = sorted(
                            self._indicator_miss_counts,
                            key=self._indicator_miss_counts.get,  # type: ignore[arg-type]
                            reverse=True,
                        )
                        _keep_set = set(_sorted[:_keep_top])
                        for _k in list(self._indicator_miss_counts):
                            if _k not in _keep_set:
                                self._indicator_miss_counts.pop(_k, None)
                    if (
                        self._indicator_miss_counts[miss_key] <= 1
                        or self._indicator_miss_counts[miss_key] % 100 == 0
                    ):
                        logger.warning(
                            "Strategy %s/%s/%s skipped: missing indicators %s (count=%d)",
                            symbol,
                            timeframe,
                            strategy,
                            missing,
                            self._indicator_miss_counts[miss_key],
                        )
                    continue
                scoped_indicators = {
                    ind: indicators[ind] for ind in required_indicators
                }
            else:
                scoped_indicators = indicators

            # Pre-flight affinity gate 在 evaluate() 前运行，直接跳过明显不匹配当前 regime 的策略。
            if min_affinity_skip > 0.0:
                affinity = self._effective_affinity(
                    strategy,
                    regime,
                    regime_metadata.get("_soft_regime"),
                    _parsed_cache=_soft_parsed,
                )
                if affinity < min_affinity_skip:
                    self._affinity_gates_skipped += 1
                    tf_skip = self._per_tf_skips.setdefault(timeframe, {"affinity": 0, "raw_conf": 0})
                    tf_skip["affinity"] += 1
                    continue
                regime_metadata["_pre_computed_affinity"] = affinity
            else:
                regime_metadata.pop("_pre_computed_affinity", None)

            with shard_lock:
                state = self._state_by_target.setdefault(
                    (symbol, timeframe, strategy), RuntimeSignalState()
                )
            if not self._is_new_snapshot(
                state,
                scope=scope,
                event_time=event_time,
                bar_time=bar_time,
                indicators=scoped_indicators,
            ):
                continue

            # 市场结构上下文只解析一次，并共享给同一快照下的所有策略评估。
            if not _structure_resolved and self._market_structure_analyzer is not None:
                _structure_resolved = True
                try:
                    structure_context = self._resolve_market_structure_context(
                        symbol=symbol,
                        timeframe=timeframe,
                        scope=scope,
                        event_time=event_time,
                        bar_time=bar_time,
                        latest_close=regime_metadata.get("close_price"),
                    )
                except Exception:
                    logger.warning(
                        "Failed to build market structure context for %s/%s",
                        symbol,
                        timeframe,
                        exc_info=True,
                    )
                    structure_context = {}
                if structure_context:
                    regime_metadata["market_structure"] = structure_context

            # Event impact forecast 命中时写入 metadata，供策略做事件前避险或降权。
            if _event_impact is not None:
                regime_metadata["_event_impact_forecast"] = _event_impact

            # HTF 指标按 INI [strategy_htf] 配置解析，并在本轮 evaluate() 时透传。
            htf_spec = self._strategy_htf_config.get(strategy)
            htf_payload: dict[str, dict[str, dict[str, Any]]] = (
                self._resolve_htf_indicators(symbol, timeframe, htf_spec)
                if self._htf_indicators_enabled and htf_spec
                else {}
            )

            decision = self.service.evaluate(
                symbol=symbol,
                timeframe=timeframe,
                strategy=strategy,
                indicators=scoped_indicators,
                metadata=regime_metadata,
                persist=False,
                htf_indicators=htf_payload,
            )
            decision = self._apply_confidence_adjustments(
                decision, symbol, timeframe, strategy, scope, regime_metadata,
            )
            snapshot_decisions.append(decision)

            # Per-TF 评估计数。
            tf_eval = self._per_tf_eval_stats.setdefault(
                timeframe, {"evaluated": 0, "hold": 0, "buy_sell": 0, "below_min_conf": 0}
            )
            tf_eval["evaluated"] += 1
            if decision.direction in ("buy", "sell"):
                tf_eval["buy_sell"] += 1
            else:
                tf_eval["hold"] += 1

            self._transition_and_publish(
                state, decision, scope, event_time, bar_time,
                regime_metadata, scoped_indicators, indicators,
            )

        return snapshot_decisions

    def _apply_confidence_adjustments(
        self,
        decision: Any,
        symbol: str,
        timeframe: str,
        strategy: str,
        scope: str,
        regime_metadata: dict[str, Any],
    ) -> Any:
        """Apply HTF alignment first, then intrabar decay.

        先应用 HTF 方向对齐的置信度修正，再对 intrabar 做衰减。
        intrabar 的降权只影响未收盘快照，不会覆盖 confirmed bar 的最终置信度。
        """
        # 1. HTF 对齐优先，先把顺势增益或逆势惩罚体现在 confidence 上。
        if decision.direction in ("buy", "sell"):
            htf_mul, htf_dir = self._compute_htf_alignment(
                symbol, timeframe, decision.direction, scope,
            )
            if htf_mul is not None:
                decision = _dc.replace(
                    decision,
                    confidence=min(1.0, decision.confidence * htf_mul),
                )
                regime_metadata["htf_direction"] = htf_dir
                regime_metadata["htf_alignment"] = (
                    "aligned" if htf_mul >= 1.0 else "conflict"
                )
                regime_metadata["htf_confidence_multiplier"] = htf_mul
        # 2. Intrabar 衰减只在 scope=intrabar 时生效，避免覆盖 HTF 调整后的 confirmed 结果。
        if scope == "intrabar":
            decay = self._strategy_intrabar_decay.get(
                strategy, self._intrabar_confidence_factor
            )
            decision = apply_intrabar_decay(decision, scope, decay)
        # 3. 对可交易方向施加最小置信度地板，防止修正后跌到接近 0 的极端值。
        if decision.confidence > 0 and decision.direction in ("buy", "sell"):
            floor = self._confidence_floor if hasattr(self, "_confidence_floor") else 0.10
            if decision.confidence < floor:
                decision = _dc.replace(decision, confidence=floor)
        return decision

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
        """驱动状态机转移、持久化 actionable 决策并发布 SignalEvent。"""
        transition_metadata = (
            self._transition_confirmed(
                state, decision.direction, event_time, bar_time, regime_metadata
            )
            if scope == "confirmed"
            else self._transition_intrabar(
                state,
                decision.direction,
                decision.confidence,
                event_time,
                bar_time,
                regime_metadata,
            )
        )
        if transition_metadata is None:
            return

        # 将策略 category 写入 metadata，供 TradeExecutor 处理 HTF 冲突豁免等执行规则。
        _strat_obj = self.service.get_strategy(decision.strategy)
        if _strat_obj is not None:
            transition_metadata["strategy_category"] = getattr(
                _strat_obj, "category", ""
            )

        # Voting group members only contribute votes; no standalone signal.
        if decision.strategy in self._voting_group_members:
            return

        signal_id = ""
        is_actionable = decision.direction in ("buy", "sell")
        if transition_metadata.get("state_changed", True):
            record = self.service.persist_decision(
                decision, indicators=scoped_indicators, metadata=transition_metadata
            )
            signal_id = record.signal_id if record is not None else uuid4().hex[:12]
        elif is_actionable:
            signal_id = uuid4().hex[:12]

        self._publish_signal_event(
            decision, signal_id, scope, full_indicators, transition_metadata
        )

    def _market_structure_lookback_bars(self, timeframe: str) -> int | None:
        analyzer = self._market_structure_analyzer
        if analyzer is None:
            return None
        analyzer_config = getattr(analyzer, "config", None)
        default_lookback = int(getattr(analyzer_config, "lookback_bars", 400))
        # M1/M5 使用更短的 lookback bars，避免市场结构分析在低周期上过重。
        if str(timeframe).strip().upper() in ("M1", "M5"):
            return max(
                2,
                int(getattr(analyzer_config, "m1_lookback_bars", 120) or 120),
            )
        return default_lookback

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
        analyzer = self._market_structure_analyzer
        if analyzer is None:
            return {}

        return analyzer.analyze_cached(
            symbol,
            timeframe,
            scope=scope,
            event_time=event_time,
            latest_close=latest_close,
            lookback_bars_override=self._market_structure_lookback_bars(timeframe),
        )

    def _get_vote_emit_kwargs(self) -> dict:
        """构造投票融合阶段所需的回调与共享状态。"""
        return dict(
            state_by_target=self._state_by_target,
            get_shard_lock=self._get_shard_lock,
            transition_confirmed_fn=self._transition_confirmed,
            transition_intrabar_fn=self._transition_intrabar,
            persist_fn=self.service.persist_decision,
            publish_fn=self._publish_signal_event,
        )

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
        # Publish the signal event assembled by the runtime.
        _do_process_voting(
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
            voting_engine=self._voting_engine,
            voting_group_engines=self._voting_group_engines,
            fusion_cache=self._vote_fusion_cache,
            **self._get_vote_emit_kwargs(),
        )

    _CONFIRMED_BURST_LIMIT = 5  # 连续处理 N 个 confirmed 后，主动让出一次机会给 intrabar。

    def _dequeue_event(self, timeout: float) -> tuple | None:
        """按 anti-starvation 策略从 confirmed/intrabar 队列中取下一条事件。"""
        try:
            event = self._confirmed_events.get_nowait()
            self._confirmed_burst_count += 1
            if self._confirmed_burst_count >= self._CONFIRMED_BURST_LIMIT:
                self._confirmed_burst_count = 0
                try:
                    intrabar_event = self._intrabar_events.get_nowait()
                    try:
                        self._confirmed_events.put_nowait(event)
                    except queue.Full:
                        logger.warning("Confirmed event queue full, dropping re-queued event")
                    event = intrabar_event
                except queue.Empty:
                    pass
            return event
        except queue.Empty:
            self._confirmed_burst_count = 0
            try:
                return self._intrabar_events.get(timeout=timeout)
            except queue.Empty:
                return None

    def _is_stale_intrabar(self, scope: str, symbol: str, timeframe: str, metadata: dict[str, Any]) -> bool:
        """判断 intrabar 事件是否已在队列中滞留过久。"""
        if scope != "intrabar":
            return False
        enqueued_raw = metadata.get("_enqueued_at")
        if enqueued_raw is None:
            return False
        try:
            queue_age = time.monotonic() - float(enqueued_raw)
            if queue_age > 300.0:
                logger.debug(
                    "Dropping stale intrabar event for %s/%s (queue_age=%.1fs)",
                    symbol, timeframe, queue_age,
                )
                return True
        except (TypeError, ValueError):
            logger.debug("Failed to parse _enqueued_at for %s/%s intrabar event", symbol, timeframe)
        return False

    def _apply_filter_chain(
        self, symbol: str, scope: str, timeframe: str,
        event_time: datetime, indicators: dict[str, dict[str, float]],
        active_sessions: list[str],
        metadata: dict[str, Any],
    ) -> bool:
        """执行 FilterChain 过滤，并同步维护按 scope 的统计窗口。"""
        if self.filter_chain is None:
            return True
        spread_points = float(metadata.get("spread_points", 0.0))
        trace_id = str(metadata.get("signal_trace_id") or "").strip()
        allowed, reason = self.filter_chain.should_evaluate(
            symbol,
            spread_points=spread_points,
            utc_now=event_time,
            active_sessions=active_sessions,
            indicators=indicators,
        )
        pipeline_bus = getattr(self, "_pipeline_event_bus", None)
        category = reason.split(":")[0] if reason else "_pass"
        if pipeline_bus is not None and trace_id:
            pipeline_bus.emit_signal_filter_decided(
                trace_id=trace_id,
                symbol=symbol,
                timeframe=timeframe,
                scope=scope,
                allowed=allowed,
                reason=reason or "",
                category=category,
                spread_points=spread_points,
                active_sessions=active_sessions,
            )
        if allowed:
            scope_stats = self._filter_by_scope.setdefault(scope, {"passed": 0, "blocked": 0, "blocks": {}})
            scope_stats["passed"] += 1
            self._filter_window.append((time.monotonic(), scope, "_pass"))
            return True

        log_fn = logger.info if scope == "confirmed" else logger.debug
        log_fn("Signal evaluation skipped for %s/%s [%s]: %s", symbol, timeframe, scope, reason)
        # 将 reason 按前缀归类，便于统计各类 filter block 的占比。
        category = reason.split(":")[0] if reason else "unknown"
        scope_stats = self._filter_by_scope.setdefault(scope, {"passed": 0, "blocked": 0, "blocks": {}})
        scope_stats["blocked"] += 1
        scope_stats["blocks"][reason] = scope_stats["blocks"].get(reason, 0) + 1
        self._filter_window.append((time.monotonic(), scope, category))
        return False

    def _detect_regime(
        self,
        indicators: dict[str, dict[str, float]],
        metadata: dict[str, Any],
        active_sessions: list[str],
    ) -> tuple[RegimeType, dict[str, Any]]:
        """检测当前 snapshot 的 regime，并回填 soft regime 等 metadata。"""
        soft_regime: SoftRegimeResult | None = None
        if self._soft_regime_enabled:
            soft_regime = self._regime_detector.detect_soft(indicators)
            regime = soft_regime.dominant_regime
        else:
            regime = self._regime_detector.detect(indicators)
        regime_metadata = dict(metadata)
        regime_metadata["_regime"] = regime.value
        if soft_regime is not None:
            regime_metadata["_soft_regime"] = soft_regime.to_dict()
            regime_metadata["regime_probabilities"] = {
                item.value: soft_regime.probability(item) for item in RegimeType
            }
        regime_metadata["session_buckets"] = list(active_sessions)
        if "close_price" not in regime_metadata:
            regime_metadata["close_price"] = extract_close_price(indicators)
        return regime, regime_metadata

    def process_next_event(self, timeout: float = 0.5) -> bool:
        """处理单个队列事件。

        优先消费 confirmed，但为了避免 confirmed 长时间独占队列，内部会受
        ``_CONFIRMED_BURST_LIMIT`` 限制，周期性插入一次 intrabar 处理机会。
        这样可以在高频 confirmed 到来时，仍让 intrabar 获得基本的调度公平性。
        """
        event = self._dequeue_event(timeout)
        if event is None:
            return False

        scope, symbol, timeframe, indicators, metadata = event
        snapshot_time = self._parse_event_time(
            metadata.get("snapshot_time", datetime.now(timezone.utc))
        )
        bar_time = self._parse_event_time(metadata.get("bar_time", snapshot_time))
        event_time = bar_time if scope == "confirmed" else snapshot_time

        if self._is_stale_intrabar(scope, symbol, timeframe, metadata):
            self._processed_events += 1
            return True

        if (
            self.filter_chain is not None
            and self.filter_chain.session_filter is not None
        ):
            active_sessions = self.filter_chain.session_filter.current_sessions(event_time)
        else:
            active_sessions = []

        if not self._apply_filter_chain(symbol, scope, timeframe, event_time, indicators, active_sessions, metadata):
            self._processed_events += 1
            self._run_count += 1
            self._last_run_at = datetime.now(timezone.utc)
            return True

        regime, regime_metadata = self._detect_regime(indicators, metadata, active_sessions)

        # 若当前 regime/scope 下没有任何策略具备可执行 affinity，直接跳过整批快照。
        if not self._any_strategy_eligible(
            symbol, timeframe, scope, regime,
            regime_metadata.get("_soft_regime"), active_sessions,
        ):
            self._processed_events += 1
            self._run_count += 1
            self._last_run_at = datetime.now(timezone.utc)
            return True

        tracker = self._regime_trackers.setdefault((symbol, timeframe), RegimeTracker())
        regime_stability = (
            tracker.update(regime) if scope == "confirmed" else tracker.stability_multiplier()
        )

        snapshot_decisions = self._evaluate_strategies(
            symbol, timeframe, scope, indicators, regime,
            regime_metadata, event_time, bar_time, active_sessions,
        )

        self._process_voting(
            snapshot_decisions, symbol, timeframe, scope, regime,
            regime_stability, regime_metadata, indicators, event_time, bar_time,
        )

        self._processed_events += 1
        self._run_count += 1
        self._last_run_at = datetime.now(timezone.utc)
        self._last_error = None
        return True

    def _compute_htf_alignment(
        self,
        symbol: str,
        timeframe: str,
        action: str,
        scope: str,
    ) -> tuple[float | None, str | None]:
        return compute_htf_alignment(
            symbol,
            timeframe,
            action,
            scope,
            htf_context_fn=self._htf_context_fn,
            htf_direction_fn=self._htf_direction_fn,
            alignment_boost=self._htf_alignment_boost,
            conflict_penalty=self._htf_conflict_penalty,
            strength_coeff=self._htf_strength_coeff,
            stability_per_bar=self._htf_stability_per_bar,
            stability_cap=self._htf_stability_cap,
            intrabar_ratio=self._htf_intrabar_ratio,
        )

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

    def _any_strategy_eligible(
        self,
        symbol: str,
        timeframe: str,
        scope: str,
        regime: RegimeType,
        soft_regime: dict[str, Any] | None,
        active_sessions: list[str],
    ) -> bool:
        """判断当前 regime/scope/session 下是否至少有一个策略值得继续评估。"""
        return _any_strategy_eligible_fn(
            symbol,
            timeframe,
            scope,
            regime,
            soft_regime,
            active_sessions,
            min_affinity_skip=self.policy.min_affinity_skip,
            target_index=self._target_index,
            strategy_sessions=self.policy.strategy_sessions,
            strategy_timeframes=self.policy.strategy_timeframes,
            strategy_scopes=self._strategy_scopes,
            strategy_affinity=self._strategy_affinity,
            soft_regime_enabled=self._soft_regime_enabled,
        )

    def _effective_affinity(
        self,
        strategy: str,
        regime: RegimeType,
        soft_regime: dict[str, Any] | None,
        *,
        _parsed_cache: SoftRegimeResult | None = None,
    ) -> float:
        return _effective_affinity_fn(
            strategy,
            regime,
            soft_regime,
            self._strategy_affinity,
            self._soft_regime_enabled,
            _parsed_cache=_parsed_cache,
        )

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
