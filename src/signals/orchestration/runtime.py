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
    Dict,
    Iterable,
    List,
    Optional,
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
            [str, str, datetime, Dict[str, Dict[str, float]], str], None
        ],
    ) -> None: ...

    def remove_snapshot_listener(
        self,
        listener: Callable[
            [str, str, datetime, Dict[str, Dict[str, float]], str], None
        ],
    ) -> None: ...


@dataclass(frozen=True)
class SignalTarget:
    symbol: str
    timeframe: str
    strategy: str


class SignalRuntime:
    """Event-driven runtime based on indicator snapshots."""

    def __init__(
        self,
        service: SignalModule,
        snapshot_source: SnapshotSource,
        targets: Iterable[SignalTarget],
        enable_confirmed_snapshot: bool = True,
        enable_intrabar: bool = False,
        policy: Optional[SignalPolicy] = None,
        filter_chain: Optional[SignalFilterChain] = None,
        regime_detector: Optional[MarketRegimeDetector] = None,
        market_structure_analyzer: Optional[Any] = None,
        htf_indicators_enabled: bool = True,
        intrabar_confidence_factor: float = 1.0,
        htf_direction_fn: Optional[Callable[[str, str], Optional[str]]] = None,
        htf_context_fn: Optional[Callable[..., Any]] = None,
        htf_conflict_penalty: float = 0.70,
        htf_alignment_boost: float = 1.10,
        htf_alignment_strength_coefficient: float = 0.30,
        htf_alignment_stability_per_bar: float = 0.03,
        htf_alignment_stability_cap: float = 1.15,
        htf_alignment_intrabar_strength_ratio: float = 0.50,
        htf_target_config: Optional[Dict[str, str]] = None,
        warmup_ready_fn: Optional[Callable[[], bool]] = None,
    ):
        self.service = service
        self.snapshot_source = snapshot_source
        self.enable_confirmed_snapshot = bool(enable_confirmed_snapshot)
        self.enable_intrabar = bool(enable_intrabar)
        self.policy = policy or SignalPolicy()
        self.filter_chain = filter_chain
        # Regime 检测器：在 process_next_event 循环开始前仅检测一次，
        # 结果通过 metadata["_regime"] 传递给 service.evaluate()，
        # 避免每个策略重复调用 detect()（N 策略 → 1次检测）。
        self._regime_detector: MarketRegimeDetector = (
            regime_detector
            or getattr(service, "_regime_detector", None)
            or MarketRegimeDetector()
        )
        self._soft_regime_enabled = bool(getattr(service, "soft_regime_enabled", False))
        self._market_structure_analyzer = market_structure_analyzer
        # 表决引擎：由 policy 配置决定是否启用。
        # voting_groups 非空时使用多组模式（全局 consensus 自动禁用）。
        # voting_groups 为空且 voting_enabled=True 时退回旧的单 consensus 行为。
        self._voting_engine: Optional[StrategyVotingEngine] = (
            StrategyVotingEngine(
                consensus_threshold=self.policy.voting_consensus_threshold,
                min_quorum=self.policy.voting_min_quorum,
                disagreement_penalty=self.policy.voting_disagreement_penalty,
            )
            if self.policy.voting_enabled and not self.policy.voting_groups
            else None
        )
        # 多组 voting 引擎列表：每个 (group_config, engine) 对应一个命名 voting group。
        self._voting_group_engines: list[tuple[Any, StrategyVotingEngine]] = (
            self._build_group_engines(self.policy)
        )
        # 属于 voting group 的策略集合（个体信号不发布，只贡献投票）
        self._voting_group_members: frozenset[str] = frozenset(
            name
            for group in self.policy.voting_groups
            for name in group.strategies
        ) - self.policy.standalone_override
        # Regime 稳定性跟踪：key=(symbol, timeframe)，每个交易对独立计数
        self._regime_trackers: dict[tuple[str, str], RegimeTracker] = {}
        self._signal_listeners: List[Callable[[SignalEvent], None]] = []
        self._signal_listeners_lock = threading.Lock()
        # Listener 熔断：连续失败 N 次后自动 deregister
        self._listener_fail_counts: Dict[int, int] = {}  # id(listener) → count
        self._LISTENER_MAX_CONSECUTIVE_FAILURES = 10
        self._targets = list(targets)
        self._target_index: dict[tuple[str, str], list[str]] = {}
        self._strategy_requirements: dict[str, tuple[str, ...]] = {}
        # Maps strategy name → frozenset of scopes it wants to receive.
        # Populated from strategy_impl.preferred_scopes; falls back to both
        # scopes for strategies that do not declare a preference.
        self._strategy_scopes: dict[str, frozenset[str]] = {}
        # 启动时缓存每个策略的 regime_affinity，避免 process_next_event 热路径中的 getattr。
        self._strategy_affinity: dict[str, dict[RegimeType, float]] = {}
        # HTF 指标配置：从 INI [strategy_htf] 解析（策略.指标 = TF）
        # 按策略分组：{strategy_name: {tf: [indicator_names...]}}
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
        # 系统中已配置的时间框架集合（用于 HTF 指标解析校验）
        self._configured_timeframes: frozenset[str] = frozenset(
            target.timeframe.upper() for target in self._targets
        )
        # HTF 指标注入全局开关
        self._htf_indicators_enabled: bool = htf_indicators_enabled
        # Intrabar 置信度缩放因子（<1.0 降权未收盘 bar 信号）
        # 全局默认值，可被策略级 strategy_params 中 *__intrabar_decay 覆盖
        self._intrabar_confidence_factor: float = min(intrabar_confidence_factor, 1.0)
        self._strategy_intrabar_decay: dict[str, float] = (
            {}
        )  # populated by set_strategy_intrabar_decay
        # HTF 方向对齐修正：在策略评估后、持久化前应用，使记录的 confidence 反映真实值
        self._htf_direction_fn = htf_direction_fn
        self._htf_context_fn = htf_context_fn
        self._htf_conflict_penalty: float = htf_conflict_penalty
        self._htf_alignment_boost: float = htf_alignment_boost
        self._htf_strength_coeff: float = htf_alignment_strength_coefficient
        self._htf_stability_per_bar: float = htf_alignment_stability_per_bar
        self._htf_stability_cap: float = htf_alignment_stability_cap
        self._htf_intrabar_ratio: float = htf_alignment_intrabar_strength_ratio
        # 显式 warmup 屏障：回调返回 True 时才允许信号评估。
        # None 表示无外部 warmup 控制（standalone / 测试场景），直接放行。
        self._warmup_ready_fn: Optional[Callable[[], bool]] = warmup_ready_fn
        # 每个 (symbol, tf) 在 warmup 结束后是否已收到首个实时 confirmed bar
        self._first_realtime_bar_seen: set[tuple[str, str]] = set()
        # 每个 (symbol, tf) 是否已收到首个含指标的 intrabar 快照
        self._first_intrabar_snapshot_seen: set[tuple[str, str]] = set()

        # R-2: 分片锁 — 热路径按 (symbol, timeframe) 分片，避免全局锁争用。
        # _state_lock 仅用于 _count_active_states() 的全量快照读取。
        self._shard_locks: dict[tuple[str, str], threading.Lock] = {}
        self._meta_lock = threading.Lock()  # 保护 _shard_locks 懒初始化

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._indicator_miss_counts: dict[tuple[str, str, str], int] = {}
        # Separate queues by scope so that intrabar bursts cannot starve
        # confirmed (bar-close) events, which must never be dropped.
        # Confirmed snapshots can briefly burst during startup catch-up after
        # ingestion backfills closed bars. Keep enough headroom to buffer that
        # replay without dropping durable bar-close signals.
        self._confirmed_events: queue.Queue = queue.Queue(maxsize=4096)
        self._intrabar_events: queue.Queue = queue.Queue(maxsize=8192)
        self._last_run_at: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._run_count = 0
        self._processed_events = 0
        self._dropped_events = 0
        self._warmup_skipped = 0
        # 保护 _state_by_target 的锁：background loop 写入，status()/API线程读取，
        # 必须避免并发迭代导致 "dictionary changed size during iteration"
        self._state_lock = threading.Lock()
        self._dropped_confirmed = 0
        self._dropped_intrabar = 0
        self._confirmed_backpressure_waits = 0
        self._confirmed_backpressure_failures = 0
        self._last_drop_log_at: float = 0.0
        self._dropped_at_last_log: int = 0  # 上次日志时的总丢弃数，用于计算 delta
        self._state_by_target: dict[tuple[str, str, str], RuntimeSignalState] = {}
        # 连续异常计数：超过阈值时发出 ERROR 级 DEGRADED 告警，
        # 提醒运维信号运行时已停滞，不依赖指数退避掩盖故障。
        self._consecutive_loop_errors = 0
        self._loop_error_alert_threshold = 5
        self._affinity_gates_skipped: int = 0
        # FilterChain 按 scope 分维度计数
        self._filter_by_scope: Dict[str, Dict[str, Any]] = {
            "confirmed": {"passed": 0, "blocked": 0, "blocks": {}},
            "intrabar": {"passed": 0, "blocked": 0, "blocks": {}},
        }
        # 滑动窗口：记录最近 1h 的 filter 事件 (timestamp, scope, "pass"|reason_category)
        self._filter_window: deque[tuple[float, str, str]] = deque()
        self._filter_window_seconds: float = 3600.0  # 1 hour
        self._filter_started_at: float = time.monotonic()
        self._htf_stale_counter: list[int] = [0]
        # 预初始化 event impact 缓存（避免竞态条件下的 getattr 延迟初始化）
        self._event_impact_cache: Dict[str, Any] = {
            "data": None,
            "expires_at": datetime.now(timezone.utc),
        }
        # Anti-starvation 计数器（预初始化，避免 getattr 延迟初始化）
        self._confirmed_burst_count: int = 0
        self._vote_fusion_cache: dict[
            tuple[str, str, datetime], dict[str, tuple[str, Any]]
        ] = {}
        # (legacy field removed — cache now lives inside MarketStructureAnalyzer)

    @staticmethod
    def _build_group_engines(
        policy: SignalPolicy,
    ) -> "list[tuple[Any, StrategyVotingEngine]]":
        """根据 policy.voting_groups 构建每组独立的 StrategyVotingEngine。"""
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
        # 属于 voting group 的策略集合（个体信号不发布，只贡献投票）
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

    def _on_snapshot(
        self,
        symbol: str,
        timeframe: str,
        bar_time: datetime,
        indicators: Dict[str, Dict[str, float]],
        scope: str,
    ) -> None:
        if scope == "confirmed" and not self.enable_confirmed_snapshot:
            return
        if scope == "intrabar" and not self.enable_intrabar:
            return
        # ── Warmup barrier: 显式阶段屏障 ──────────────────────────
        # warmup 期间（回补未完成）所有快照仅用于填充指标缓存，
        # 不入队、不评估、不产生信号。confirmed 和 intrabar 都被屏蔽。
        # 回补结束后还需要收到该 (symbol, tf) 的首个实时 confirmed bar
        # 才解除屏障，确保策略基于完全新鲜的数据做出首次判断。
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
                # snapshot processing is async — stale backfill snapshots
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
                    # Allow up to 2× timeframe duration + 30s processing margin
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
            # Indicator readiness: ATR is required for trade execution.
            # If the snapshot doesn't include atr14, indicators haven't
            # fully warmed up yet — skip to avoid wasting the first
            # state_changed=true transition on an un-executable signal.
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
                # confirmed 首个实时 bar 尚未到达，跳过 intrabar
                return
            # 首次 intrabar 快照须包含足够指标（pipeline 已完成完整一轮计算），
            # 否则策略评估时 intrabar 指标缺失，产生无意义的 warning。
            # 判断标准：快照能满足至少一个 intrabar 策略的全部 required_indicators。
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
            except Exception:
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
        item: tuple[str, str, str, Dict[str, Dict[str, float]], Dict[str, Any]],
    ) -> None:
        # 标记入队时间（monotonic），供消费端检测僵尸事件
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
                    # confirmed 事件优先保证可靠性：短暂阻塞等待消费者腾挪队列。
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
                # confirmed 事件丢弃是严重问题，每次都记录 WARNING
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
                    "Signal runtime %s queue is full — indicator snapshot dropped "
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
        """计算最近 1h 滑动窗口内的 filter 通过/拦截统计（按 scope 分维度）。"""
        now = time.monotonic()
        cutoff = now - self._filter_window_seconds
        while self._filter_window and self._filter_window[0][0] < cutoff:
            self._filter_window.popleft()

        by_scope: Dict[str, Dict[str, Any]] = {}
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
        """持锁快照 _state_by_target，统计活跃的 preview/confirmed 状态数。"""
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
            # 持锁做快照再迭代，避免 background loop 同时写入导致 RuntimeError
            **self._count_active_states(),
        }

    def get_regime_stability(
        self, symbol: str, timeframe: str
    ) -> Optional[dict[str, Any]]:
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
        """Set per-strategy intrabar decay overrides (from strategy_params *__intrabar_decay)."""
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
        """Register a callback to receive SignalEvent on every state transition."""
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
        indicators: Dict[str, Dict[str, float]],
        transition_metadata: Dict[str, Any],
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
        listeners_to_remove: List[Callable] = []
        for listener in listeners:
            lid = id(listener)
            try:
                listener(event)
                # 成功时重置失败计数
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
        metadata: Dict[str, Any],
        *,
        signal_state: str,
        state_changed: bool,
        previous_state: str,
    ) -> Dict[str, Any]:
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
    def _snapshot_signature(indicators: Dict[str, Dict[str, float]]) -> int:
        return _sm_snapshot_signature(indicators)

    def _is_new_snapshot(
        self,
        state: RuntimeSignalState,
        *,
        scope: str,
        event_time: datetime,
        bar_time: datetime,
        indicators: Dict[str, Dict[str, float]],
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
        metadata: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
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
        metadata: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
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
        self, indicators: Dict[str, Dict[str, float]]
    ) -> bool:
        """检查快照是否能满足所有 intrabar 策略的 required_indicators。

        在 warmup 过渡期，intrabar pipeline 可能分批发布部分指标快照。
        只有当快照包含所有 intrabar 策略所需的指标时才放行，
        避免部分策略因缺失指标产生无意义的 warning。
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
        """懒初始化并返回 (symbol, timeframe) 对应的分片锁。

        双重检查锁定（Double-Checked Locking）模式：
        - 热路径（读）无需 meta_lock；仅在首次创建时持锁。
        - 保证不同 (symbol, timeframe) 对的状态写入可独立进行。
        """
        key = (symbol, timeframe)
        lock = self._shard_locks.get(key)
        if lock is None:
            with self._meta_lock:
                lock = self._shard_locks.get(key)
                if lock is None:
                    lock = threading.Lock()
                    self._shard_locks[key] = lock
        return lock

    def _evaluate_strategies(
        self,
        symbol: str,
        timeframe: str,
        scope: str,
        indicators: Dict[str, Dict[str, float]],
        regime: RegimeType,
        regime_metadata: Dict[str, Any],
        event_time: datetime,
        bar_time: datetime,
        active_sessions: List[str],
    ) -> List:
        """评估该 snapshot 下所有策略，返回收集到的 SignalDecision 列表。

        职责：
        1. 筛选适合当前 scope / regime 的策略（affinity gate）
        2. 收窄 scoped_indicators
        3. 去重检查（snapshot signature）
        4. 调用 service.evaluate()
        5. 状态机转换（transition_confirmed / transition_intrabar）
        6. 持久化 + 发布信号事件
        """
        snapshot_decisions: List = []
        min_affinity_skip = self.policy.min_affinity_skip
        # 延迟市场结构分析：仅在第一个策略真正需要评估时按需计算一次
        _structure_resolved = False
        strategies = self._target_index.get((symbol, timeframe), [])
        shard_lock = self._get_shard_lock(symbol, timeframe)

        # Pre-parse soft regime once for all strategies in this snapshot
        _soft_parsed: Optional[SoftRegimeResult] = None
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

        # 查询 event impact forecast（带 5 分钟 TTL 缓存，避免每 bar 查 DB）
        _event_impact: Optional[Dict[str, Any]] = None
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
                    # 低频日志：每个 (symbol, tf, strategy) 组合每 100 次缺失只记录一次
                    miss_key = (symbol, timeframe, strategy)
                    self._indicator_miss_counts[miss_key] = (
                        self._indicator_miss_counts.get(miss_key, 0) + 1
                    )
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

            # Pre-flight affinity gate — 计算结果传递给 evaluate() 复用
            if min_affinity_skip > 0.0:
                affinity = self._effective_affinity(
                    strategy,
                    regime,
                    regime_metadata.get("_soft_regime"),
                    _parsed_cache=_soft_parsed,
                )
                if affinity < min_affinity_skip:
                    self._affinity_gates_skipped += 1
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

            # ── 延迟市场结构分析（仅首次触发时计算）──────────
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

            # ── Event impact forecast 注入 ──
            if _event_impact is not None:
                regime_metadata["_event_impact_forecast"] = _event_impact

            # ── HTF 指标注入（按 INI [strategy_htf] 配置按需查询）──
            htf_spec = self._strategy_htf_config.get(strategy)
            htf_payload: Dict[str, Dict[str, Dict[str, Any]]] = (
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
            # ── Intrabar 置信度衰减（策略级覆盖 > 全局默认）──
            if scope == "intrabar":
                decay = self._strategy_intrabar_decay.get(
                    strategy, self._intrabar_confidence_factor
                )
                decision = apply_intrabar_decay(decision, scope, decay)
            # ── HTF 方向对齐修正 ──────────────────────────
            if decision.direction in ("buy", "sell"):
                htf_mul, htf_dir = self._compute_htf_alignment(
                    symbol,
                    timeframe,
                    decision.direction,
                    scope,
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
            snapshot_decisions.append(decision)

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
                continue

            # Voting group 成员：只贡献投票，不单独发布/持久化信号。
            # 投票结果由 _process_voting 统一发布为 group 信号。
            if decision.strategy in self._voting_group_members:
                continue

            # Only persist state-changing events to DB (skip repeated same-state signals).
            signal_id = ""
            is_actionable = decision.direction in ("buy", "sell")
            if transition_metadata.get("state_changed", True):
                record = self.service.persist_decision(
                    decision, indicators=scoped_indicators, metadata=transition_metadata
                )
                if record is not None:
                    signal_id = record.signal_id
                else:
                    signal_id = uuid4().hex[:12]
            elif is_actionable:
                signal_id = uuid4().hex[:12]

            self._publish_signal_event(
                decision, signal_id, scope, indicators, transition_metadata
            )

        return snapshot_decisions

    def _market_structure_lookback_bars(self, timeframe: str) -> Optional[int]:
        analyzer = self._market_structure_analyzer
        if analyzer is None:
            return None
        analyzer_config = getattr(analyzer, "config", None)
        default_lookback = int(getattr(analyzer_config, "lookback_bars", 400))
        # 低时间框架（M5）使用缩减的 lookback bars 以控制计算量
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
        latest_close: Optional[float],
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
        """Build keyword arguments for vote_processor functions."""
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
        snapshot_decisions: List,
        symbol: str,
        timeframe: str,
        scope: str,
        regime: RegimeType,
        regime_stability: float,
        regime_metadata: Dict[str, Any],
        indicators: Dict[str, Dict[str, float]],
        event_time: datetime,
        bar_time: datetime,
    ) -> None:
        """跨策略表决：委托给 vote_processor 模块。"""
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

    _CONFIRMED_BURST_LIMIT = 5  # 每连续消费 N 个 confirmed 后让出检查 intrabar

    def process_next_event(self, timeout: float = 0.5) -> bool:
        """从队列取一个快照事件并完整处理。

        优先消费 confirmed 队列，但每连续 ``_CONFIRMED_BURST_LIMIT`` 个
        confirmed 事件后主动检查 intrabar 队列取一个事件作为本轮处理对象，
        防止 intrabar 策略在 confirmed 突发期间长时间饥饿。
        """
        # 优先排空 confirmed 队列
        try:
            event = self._confirmed_events.get_nowait()
            self._confirmed_burst_count += 1
            # Anti-starvation: after N consecutive confirmed, yield to intrabar
            if self._confirmed_burst_count >= self._CONFIRMED_BURST_LIMIT:
                self._confirmed_burst_count = 0
                try:
                    # Peek intrabar: if available, put confirmed back and process intrabar
                    intrabar_event = self._intrabar_events.get_nowait()
                    try:
                        self._confirmed_events.put_nowait(event)
                    except queue.Full:
                        logger.warning(
                            "Confirmed event queue full, dropping re-queued event",
                        )
                    event = intrabar_event
                except queue.Empty:
                    pass  # No intrabar pending, continue with confirmed
        except queue.Empty:
            self._confirmed_burst_count = 0
            try:
                event = self._intrabar_events.get(timeout=timeout)
            except queue.Empty:
                return False

        scope, symbol, timeframe, indicators, metadata = event
        snapshot_time = self._parse_event_time(
            metadata.get("snapshot_time", datetime.now(timezone.utc))
        )
        bar_time = self._parse_event_time(metadata.get("bar_time", snapshot_time))
        event_time = bar_time if scope == "confirmed" else snapshot_time

        # 过期事件清理：丢弃队列中停留过久的 intrabar 事件（可能是恢复后的僵尸事件）
        # 基于 metadata 中的 _enqueued_at 时间戳（入队时间），而非 snapshot_time
        # 以避免测试或历史回放场景中的误判
        _enqueued_at_raw = metadata.get("_enqueued_at")
        if scope == "intrabar" and _enqueued_at_raw is not None:
            _MAX_INTRABAR_AGE_SECONDS = 300.0
            try:
                enqueued_at = float(_enqueued_at_raw)
                queue_age = time.monotonic() - enqueued_at
                if queue_age > _MAX_INTRABAR_AGE_SECONDS:
                    logger.debug(
                        "Dropping stale intrabar event for %s/%s (queue_age=%.1fs)",
                        symbol,
                        timeframe,
                        queue_age,
                    )
                    self._processed_events += 1
                    return True
            except (TypeError, ValueError):
                logger.debug(
                    "Failed to parse _enqueued_at for %s/%s intrabar event",
                    symbol,
                    timeframe,
                )
        if (
            self.filter_chain is not None
            and self.filter_chain.session_filter is not None
        ):
            active_sessions = self.filter_chain.session_filter.current_sessions(
                event_time
            )
        else:
            active_sessions = []

        if self.filter_chain is not None:
            spread_points = float(metadata.get("spread_points", 0.0))
            allowed, reason = self.filter_chain.should_evaluate(
                symbol,
                spread_points=spread_points,
                utc_now=event_time,
                active_sessions=active_sessions,
                indicators=indicators,
            )
            if not allowed:
                log_fn = logger.info if scope == "confirmed" else logger.debug
                log_fn(
                    "Signal evaluation skipped for %s/%s [%s]: %s",
                    symbol, timeframe, scope, reason,
                )
                # 按 scope + reason prefix 分类计数
                category = reason.split(":")[0] if reason else "unknown"
                scope_stats = self._filter_by_scope.setdefault(scope, {"passed": 0, "blocked": 0, "blocks": {}})
                scope_stats["blocked"] += 1
                scope_stats["blocks"][category] = scope_stats["blocks"].get(category, 0) + 1
                self._filter_window.append((time.monotonic(), scope, category))
                self._processed_events += 1
                self._run_count += 1
                self._last_run_at = datetime.now(timezone.utc)
                return True

        scope_stats = self._filter_by_scope.setdefault(scope, {"passed": 0, "blocked": 0, "blocks": {}})
        scope_stats["passed"] += 1
        self._filter_window.append((time.monotonic(), scope, "_pass"))

        # ── Regime 检测：每次 snapshot 仅检测一次，结果共享给所有策略 ──────
        soft_regime: Optional[SoftRegimeResult] = None
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
        # close_price 注入：在 scoped_indicators 收窄前从全量快照提取
        if "close_price" not in regime_metadata:
            regime_metadata["close_price"] = extract_close_price(indicators)

        # ── 快速全拒绝检查：如果没有任何策略能通过 affinity 门控，跳过后续所有重计算
        if not self._any_strategy_eligible(
            symbol,
            timeframe,
            scope,
            regime,
            regime_metadata.get("_soft_regime"),
            active_sessions,
        ):
            self._processed_events += 1
            self._run_count += 1
            self._last_run_at = datetime.now(timezone.utc)
            return True

        # ── Regime 稳定性追踪 ──────────────────────────────────────────────
        tracker = self._regime_trackers.setdefault((symbol, timeframe), RegimeTracker())
        regime_stability = (
            tracker.update(regime)
            if scope == "confirmed"
            else tracker.stability_multiplier()
        )

        # ── 策略评估（含持久化 + 发布）─────────────────────────────────────
        snapshot_decisions = self._evaluate_strategies(
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

        # ── 跨策略表决（consensus 信号）────────────────────────────────────
        self._process_voting(
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
    ) -> tuple[Optional[float], Optional[str]]:
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
        raw: Dict[str, str],
    ) -> dict[str, dict[str, list[str]]]:
        return parse_htf_config(raw)

    def _resolve_htf_indicators(
        self,
        symbol: str,
        current_tf: str,
        htf_spec: dict[str, list[str]],
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
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
    ) -> Optional[Dict[str, Any]]:
        """通过 snapshot_source（IndicatorManager）查询已缓存的 HTF 指标。"""
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
        soft_regime: Optional[Dict[str, Any]],
        active_sessions: List[str],
    ) -> bool:
        """Quick check: delegates to affinity module."""
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
        soft_regime: Optional[Dict[str, Any]],
        *,
        _parsed_cache: Optional[SoftRegimeResult] = None,
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
        """主事件循环，带指数退避以防止异常风暴。

        正常情况：每次 process_next_event 成功后退避重置为 0。
        异常情况：等待时间翻倍（1→2→4→...→30 秒上限），让错误有喘息时间。
        """
        backoff: float = 0.0
        _session_check_counter = 0
        while not self._stop.is_set():
            try:
                self.process_next_event(timeout=0.5)
                backoff = 0.0  # 成功后重置退避
                self._consecutive_loop_errors = 0
                # 每 200 次事件检查一次 PerformanceTracker session 重置 + 内存清理
                _session_check_counter += 1
                if _session_check_counter >= 200:
                    _session_check_counter = 0
                    perf_tracker = getattr(self.service, "_performance_tracker", None)
                    if perf_tracker is not None:
                        perf_tracker.check_session_reset()
                    # 定期清理 indicator miss 计数器，防止无界增长
                    # 保留计数最高的条目（最频繁缺失的指标值得持续告警）
                    _MAX_MISS_KEYS = 500
                    if len(self._indicator_miss_counts) > _MAX_MISS_KEYS:
                        sorted_keys = sorted(
                            self._indicator_miss_counts,
                            key=self._indicator_miss_counts.get,  # type: ignore[arg-type]
                        )
                        for k in sorted_keys[: len(sorted_keys) - _MAX_MISS_KEYS]:
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
