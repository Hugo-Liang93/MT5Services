from __future__ import annotations

import dataclasses as _dc
import logging
import queue
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
from ..evaluation.indicators_helpers import get_close
from ..evaluation.regime import (
    MarketRegimeDetector,
    RegimeTracker,
    RegimeType,
    SoftRegimeResult,
)
from ..execution.filters import SignalFilterChain
from ..models import SignalEvent
from ..service import SignalModule
from .policy import RuntimeSignalState, SignalPolicy
from .voting import StrategyVotingEngine

if TYPE_CHECKING:
    from src.indicators.manager import UnifiedIndicatorManager

logger = logging.getLogger(__name__)


def _extract_close_price(indicators: Dict[str, Dict[str, float]]) -> Optional[float]:
    """从完整指标快照中提取收盘价（委托给 indicators_helpers.get_close）。"""
    return get_close(indicators)


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
        intrabar_confidence_decay: float = 1.0,
        htf_direction_fn: Optional[Callable[[str, str], Optional[str]]] = None,
        htf_context_fn: Optional[Callable[..., Any]] = None,
        htf_conflict_penalty: float = 0.70,
        htf_alignment_boost: float = 1.10,
        htf_alignment_strength_coefficient: float = 0.30,
        htf_alignment_stability_per_bar: float = 0.03,
        htf_alignment_stability_cap: float = 1.15,
        htf_alignment_intrabar_strength_ratio: float = 0.50,
        htf_target_config: Optional[Dict[str, str]] = None,
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
        self._soft_regime_enabled = bool(
            getattr(service, "soft_regime_enabled", False)
        )
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
        self._voting_group_engines: list[
            tuple[Any, StrategyVotingEngine]
        ] = self._build_group_engines(self.policy)
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
        # Intrabar 置信度衰减因子（<1.0 降权未收盘 bar 信号）
        # 全局默认值，可被策略级 strategy_params 中 *__intrabar_decay 覆盖
        self._intrabar_confidence_decay: float = min(intrabar_confidence_decay, 1.0)
        self._strategy_intrabar_decay: dict[str, float] = {}  # populated by set_strategy_intrabar_decay
        # HTF 方向对齐修正：在策略评估后、持久化前应用，使记录的 confidence 反映真实值
        self._htf_direction_fn = htf_direction_fn
        self._htf_context_fn = htf_context_fn
        self._htf_conflict_penalty: float = htf_conflict_penalty
        self._htf_alignment_boost: float = htf_alignment_boost
        self._htf_strength_coeff: float = htf_alignment_strength_coefficient
        self._htf_stability_per_bar: float = htf_alignment_stability_per_bar
        self._htf_stability_cap: float = htf_alignment_stability_cap
        self._htf_intrabar_ratio: float = htf_alignment_intrabar_strength_ratio

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
        self._htf_stale_warnings: int = 0
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
        metadata = {
            "scope": scope,
            "bar_time": bar_time.isoformat(),
            "snapshot_time": datetime.now(timezone.utc).isoformat(),
            "trigger_source": f"{scope}_snapshot",
            "signal_trace_id": uuid4().hex,
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
                pass
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
                    symbol, timeframe,
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
            "htf_stale_warnings": self._htf_stale_warnings,
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
            action=decision.action,
            confidence=decision.confidence,
            signal_state=signal_state,
            scope=scope,
            indicators=indicators,
            metadata=transition_metadata,
            generated_at=decision.timestamp,
            signal_id=signal_id,
            reason=decision.reason,
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
                listener_name = getattr(listener, '__name__', repr(listener))
                error_msg = f"Signal listener error [{listener_name}]: {exc} (failures={fail_count})"
                self._last_error = error_msg
                logger.error(error_msg, exc_info=True)
                if fail_count >= self._LISTENER_MAX_CONSECUTIVE_FAILURES:
                    logger.error(
                        "LISTENER CIRCUIT BREAK: %s reached %d consecutive failures, "
                        "auto-deregistering to prevent cascading errors",
                        listener_name, fail_count,
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

    def _build_transition_metadata(
        self,
        metadata: Dict[str, Any],
        *,
        signal_state: str,
        state_changed: bool,
        previous_state: str,
    ) -> Dict[str, Any]:
        enriched = dict(metadata)
        enriched["signal_state"] = signal_state
        enriched["state_changed"] = state_changed
        enriched["previous_state"] = previous_state
        return enriched

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

    def _should_emit(
        self,
        state: RuntimeSignalState,
        signal_state: str,
        event_time: datetime,
        bar_time: datetime,
        *,
        cooldown_seconds: float,
    ) -> bool:
        if state.last_emitted_state != signal_state:
            return True
        if state.last_emitted_bar_time != bar_time:
            return True
        if state.last_emitted_at is None:
            return True
        return (event_time - state.last_emitted_at).total_seconds() >= cooldown_seconds

    def _mark_emitted(
        self,
        state: RuntimeSignalState,
        signal_state: str,
        event_time: datetime,
        bar_time: datetime,
    ) -> None:
        state.last_emitted_state = signal_state
        state.last_emitted_at = event_time
        state.last_emitted_bar_time = bar_time

    @staticmethod
    def _snapshot_signature(indicators: Dict[str, Dict[str, float]]) -> int:
        """O(n) hash：用 frozenset 替代 O(n log n) sorted tuple，热路径性能提升 30-50%。"""
        return hash(
            frozenset(
                (name, frozenset(payload.items()))
                for name, payload in indicators.items()
                if isinstance(payload, dict)
            )
        )

    def _should_evaluate_snapshot(
        self,
        state: RuntimeSignalState,
        *,
        scope: str,
        event_time: datetime,
        bar_time: datetime,
        indicators: Dict[str, Dict[str, float]],
    ) -> bool:
        signature = self._snapshot_signature(indicators)
        if (
            state.last_snapshot_scope == scope
            and state.last_snapshot_bar_time == bar_time
            and state.last_snapshot_signature == signature
        ):
            previous_snapshot_time = state.last_snapshot_time
            if scope == "confirmed":
                return False
            if previous_snapshot_time is not None:
                elapsed = abs((event_time - previous_snapshot_time).total_seconds())
                if elapsed < self.policy.snapshot_dedupe_window_seconds:
                    return False
        state.last_snapshot_scope = scope
        state.last_snapshot_bar_time = bar_time
        state.last_snapshot_signature = signature
        state.last_snapshot_time = event_time
        return True

    def _transition_confirmed(
        self,
        state: RuntimeSignalState,
        decision_action: str,
        event_time: datetime,
        bar_time: datetime,
        metadata: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        previous_state = state.confirmed_state
        # 在清除 preview 状态前保存快照，供 TradeExecutor require_armed 检查使用。
        # confirmed 事件的 previous_state 是上一次 confirmed_state（idle/confirmed_buy 等），
        # 永远不含 "armed"，若不单独传递 preview 状态则 require_armed=True 会阻断所有首次自动交易。
        preview_state_at_close = state.preview_state
        state.preview_state = "idle"
        state.preview_action = None
        state.preview_since = None
        state.preview_bar_time = None

        if decision_action not in {"buy", "sell"}:
            if previous_state != "idle":
                signal_state = "confirmed_cancelled"
                state.confirmed_state = "idle"
                state.confirmed_bar_time = bar_time
                self._mark_emitted(state, signal_state, event_time, bar_time)
                result = self._build_transition_metadata(
                    metadata,
                    signal_state=signal_state,
                    state_changed=True,
                    previous_state=previous_state,
                )
                result["preview_state_at_close"] = preview_state_at_close
                return result
            state.confirmed_state = "idle"
            state.confirmed_bar_time = bar_time
            return None

        signal_state = f"confirmed_{decision_action}"
        state.confirmed_state = signal_state
        state.confirmed_bar_time = bar_time
        if not self._should_emit(
            state, signal_state, event_time, bar_time, cooldown_seconds=0.0
        ):
            return None
        self._mark_emitted(state, signal_state, event_time, bar_time)
        result = self._build_transition_metadata(
            metadata,
            signal_state=signal_state,
            state_changed=signal_state != previous_state,
            previous_state=previous_state,
        )
        result["preview_state_at_close"] = preview_state_at_close
        return result

    def _transition_preview(
        self,
        state: RuntimeSignalState,
        decision_action: str,
        confidence: float,
        event_time: datetime,
        bar_time: datetime,
        metadata: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        previous_state = state.preview_state
        if state.preview_bar_time is not None and state.preview_bar_time != bar_time:
            state.preview_state = "idle"
            state.preview_action = None
            state.preview_since = None

        bar_progress = float(metadata.get("bar_progress", 0.0) or 0.0)
        actionable = (
            decision_action in {"buy", "sell"}
            and confidence >= self.policy.min_preview_confidence
            and bar_progress >= self.policy.min_preview_bar_progress
        )
        state.preview_bar_time = bar_time

        if not actionable:
            if previous_state == "idle":
                return None
            state.preview_state = "idle"
            state.preview_action = None
            state.preview_since = None
            signal_state = "cancelled"
            if not self._should_emit(
                state,
                signal_state,
                event_time,
                bar_time,
                cooldown_seconds=self.policy.preview_cooldown_seconds,
            ):
                return None
            self._mark_emitted(state, signal_state, event_time, bar_time)
            return self._build_transition_metadata(
                metadata,
                signal_state=signal_state,
                state_changed=True,
                previous_state=previous_state,
            )

        if state.preview_action != decision_action:
            state.preview_action = decision_action
            state.preview_since = event_time
            state.preview_state = f"preview_{decision_action}"
            signal_state = state.preview_state
            if not self._should_emit(
                state,
                signal_state,
                event_time,
                bar_time,
                cooldown_seconds=self.policy.preview_cooldown_seconds,
            ):
                return None
            self._mark_emitted(state, signal_state, event_time, bar_time)
            return self._build_transition_metadata(
                metadata,
                signal_state=signal_state,
                state_changed=signal_state != previous_state,
                previous_state=previous_state,
            )

        if state.preview_since is None:
            state.preview_since = event_time
            return None

        stable_seconds = (event_time - state.preview_since).total_seconds()
        if stable_seconds < self.policy.min_preview_stable_seconds:
            return None

        signal_state = f"armed_{decision_action}"
        if state.preview_state == signal_state:
            return None

        state.preview_state = signal_state
        if not self._should_emit(
            state,
            signal_state,
            event_time,
            bar_time,
            cooldown_seconds=self.policy.preview_cooldown_seconds,
        ):
            return None
        self._mark_emitted(state, signal_state, event_time, bar_time)
        enriched = self._build_transition_metadata(
            metadata,
            signal_state=signal_state,
            state_changed=signal_state != previous_state,
            previous_state=previous_state,
        )
        enriched["preview_stable_seconds"] = stable_seconds
        return enriched

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
        5. 状态机转换（transition_confirmed / transition_preview）
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
                _soft_parsed = SoftRegimeResult.from_dict(regime_metadata["_soft_regime"])
            except Exception:
                logger.info(
                    "Failed to parse soft regime for %s/%s, falling back to hard regime",
                    symbol, timeframe, exc_info=True,
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
                        upcoming_events = eco_service.get_high_impact_events(hours=2, limit=1)
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
                    if self._indicator_miss_counts[miss_key] <= 1 or self._indicator_miss_counts[miss_key] % 100 == 0:
                        logger.warning(
                            "Strategy %s/%s/%s skipped: missing indicators %s (count=%d)",
                            symbol, timeframe, strategy, missing, self._indicator_miss_counts[miss_key],
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
                    strategy, regime, regime_metadata.get("_soft_regime"),
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
            if not self._should_evaluate_snapshot(
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
                        symbol, timeframe, exc_info=True,
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
                    strategy, self._intrabar_confidence_decay
                )
                decision = apply_intrabar_decay(decision, scope, decay)
            # ── HTF 方向对齐修正 ──────────────────────────
            if decision.action in ("buy", "sell"):
                htf_mul, htf_dir = self._compute_htf_alignment(
                    symbol, timeframe, decision.action, scope,
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
                    state, decision.action, event_time, bar_time, regime_metadata
                )
                if scope == "confirmed"
                else self._transition_preview(
                    state,
                    decision.action,
                    decision.confidence,
                    event_time,
                    bar_time,
                    regime_metadata,
                )
            )
            if transition_metadata is None:
                continue

            record = self.service.persist_decision(
                decision, indicators=scoped_indicators, metadata=transition_metadata
            )
            signal_id = record.signal_id if record is not None else ""
            # 发布事件时携带全量 indicators（而非 scoped），
            # 使 TradeExecutor 等监听器能访问 ATR 等非策略核心但下单必需的指标。
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
        if str(timeframe).strip().upper() == "M1":
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

    def _emit_vote_signal(
        self,
        vote_result: Any,
        group_name: str,
        symbol: str,
        timeframe: str,
        scope: str,
        regime_stability: float,
        regime_metadata: Dict[str, Any],
        indicators: Dict[str, Dict[str, float]],
        event_time: datetime,
        bar_time: datetime,
    ) -> None:
        """对单个 vote 结果信号执行状态机转换、持久化和事件发布。"""
        adjusted_conf = min(1.0, vote_result.confidence * regime_stability)
        vote_result = _dc.replace(
            vote_result,
            confidence=adjusted_conf,
            metadata={
                **vote_result.metadata,
                "regime_stability_multiplier": round(regime_stability, 4),
            },
        )
        group_key = (symbol, timeframe, group_name)
        shard_lock = self._get_shard_lock(symbol, timeframe)
        with shard_lock:
            group_state = self._state_by_target.setdefault(
                group_key, RuntimeSignalState()
            )
        transition_metadata = (
            self._transition_confirmed(
                group_state, vote_result.action, event_time, bar_time, regime_metadata
            )
            if scope == "confirmed"
            else self._transition_preview(
                group_state,
                vote_result.action,
                vote_result.confidence,
                event_time,
                bar_time,
                regime_metadata,
            )
        )
        if transition_metadata is not None:
            record = self.service.persist_decision(
                vote_result, indicators=indicators, metadata=transition_metadata
            )
            signal_id = record.signal_id if record is not None else ""
            self._publish_signal_event(
                vote_result, signal_id, scope, indicators, transition_metadata
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
        """跨策略表决：根据配置模式发出命名 voting group 信号或全局 consensus 信号。

        多组模式（voting_groups 非空）：
            每个 group 仅对其成员策略的决策投票，产生以 group.name 命名的信号。
            全局 consensus 在此模式下自动禁用。

        单 consensus 模式（backward compatible）：
            所有独立策略参与投票，产生 strategy="consensus" 信号。
        """
        if not snapshot_decisions:
            return
        snapshot_decisions = self._fuse_vote_decisions(
            symbol,
            timeframe,
            bar_time,
            scope,
            snapshot_decisions,
        )
        if not snapshot_decisions:
            return

        # ── 多组模式 ──────────────────────────────────────────────────
        if self._voting_group_engines:
            for group_config, group_engine in self._voting_group_engines:
                # 只取属于本 group 的成员策略决策
                group_decisions = [
                    d
                    for d in snapshot_decisions
                    if d.strategy in group_config.strategies
                ]
                if not group_decisions:
                    continue
                vote_result = group_engine.vote(
                    group_decisions,
                    regime=regime,
                    scope=scope,
                    exclude_composite=False,  # 分组明确指定成员，不需要自动排除复合策略
                )
                if vote_result is None:
                    continue
                self._emit_vote_signal(
                    vote_result, group_config.name,
                    symbol, timeframe, scope,
                    regime_stability, regime_metadata, indicators,
                    event_time, bar_time,
                )
            return

        # ── 单 consensus 模式（backward compatible）───────────────────
        if self._voting_engine is None:
            return
        consensus = self._voting_engine.vote(
            snapshot_decisions, regime=regime, scope=scope
        )
        if consensus is None:
            return
        self._emit_vote_signal(
            consensus, self._voting_engine.CONSENSUS_STRATEGY_NAME,
            symbol, timeframe, scope,
            regime_stability, regime_metadata, indicators,
            event_time, bar_time,
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
                        pass  # confirmed already processed, drop
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
                        symbol, timeframe, queue_age,
                    )
                    self._processed_events += 1
                    return True
            except (TypeError, ValueError):
                pass
        if self.filter_chain is not None and self.filter_chain.session_filter is not None:
            active_sessions = self.filter_chain.session_filter.current_sessions(event_time)
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
                logger.debug(
                    "Signal evaluation skipped for %s/%s: %s", symbol, timeframe, reason
                )
                self._processed_events += 1
                self._run_count += 1
                self._last_run_at = datetime.now(timezone.utc)
                return True

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
                item.value: soft_regime.probability(item)
                for item in RegimeType
            }
        regime_metadata["session_buckets"] = list(active_sessions)
        # close_price 注入：在 scoped_indicators 收窄前从全量快照提取
        if "close_price" not in regime_metadata:
            regime_metadata["close_price"] = _extract_close_price(indicators)

        # ── 快速全拒绝检查：如果没有任何策略能通过 affinity 门控，跳过后续所有重计算
        if not self._any_strategy_eligible(
            symbol, timeframe, scope, regime,
            regime_metadata.get("_soft_regime"), active_sessions,
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
            symbol, timeframe, scope, indicators,
            regime, regime_metadata, event_time, bar_time, active_sessions,
        )

        # ── 跨策略表决（consensus 信号）────────────────────────────────────
        self._process_voting(
            snapshot_decisions, symbol, timeframe, scope,
            regime, regime_stability, regime_metadata, indicators, event_time, bar_time,
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
        """Compute HTF alignment multiplier with strength weighting.

        Returns ``(None, None)`` when no HTF data is available.
        Uses enriched context (confidence, regime, stable_bars) when
        ``htf_context_fn`` is provided; otherwise falls back to the
        simple direction-only ``htf_direction_fn``.
        """
        # Try enriched context first
        if self._htf_context_fn is not None:
            ctx = self._htf_context_fn(symbol, timeframe)
            if ctx is not None:
                aligned = ctx.direction == action
                base = self._htf_alignment_boost if aligned else self._htf_conflict_penalty

                # Strength weighting: high-confidence HTF signal amplifies effect
                strength = 1.0 + (ctx.confidence - 0.5) * self._htf_strength_coeff
                # Stability weighting: direction held for more bars amplifies effect
                stability = min(
                    1.0 + (ctx.stable_bars - 1) * self._htf_stability_per_bar,
                    self._htf_stability_cap,
                )

                raw_mul = base * strength * stability

                # Intrabar: reduced-strength modification (signal not yet confirmed)
                if scope == "intrabar":
                    raw_mul = 1.0 + (raw_mul - 1.0) * self._htf_intrabar_ratio

                return raw_mul, ctx.direction

        # Fallback: simple direction-only
        if self._htf_direction_fn is not None:
            htf_dir = self._htf_direction_fn(symbol, timeframe)
            if htf_dir is not None:
                base = (
                    self._htf_alignment_boost
                    if htf_dir == action
                    else self._htf_conflict_penalty
                )
                if scope == "intrabar":
                    base = 1.0 + (base - 1.0) * self._htf_intrabar_ratio
                return base, htf_dir

        return None, None

    @staticmethod
    def _parse_htf_config(
        raw: Dict[str, str],
    ) -> dict[str, dict[str, list[str]]]:
        """解析 INI ``[strategy_htf]`` 为按策略分组的 {strategy: {tf: [indicators]}}。

        INI 格式: ``strategy.indicator = TF``
        例: ``supertrend.adx14 = H1`` → {"supertrend": {"H1": ["adx14"]}}
        """
        result: dict[str, dict[str, list[str]]] = {}
        for compound_key, tf_value in raw.items():
            parts = compound_key.split(".", 1)
            if len(parts) != 2:
                continue
            strategy_name, indicator_name = parts[0].strip(), parts[1].strip()
            tf = tf_value.strip().upper()
            if not strategy_name or not indicator_name or not tf:
                continue
            result.setdefault(strategy_name, {}).setdefault(tf, []).append(indicator_name)
        return result

    def _resolve_htf_indicators(
        self,
        symbol: str,
        current_tf: str,
        htf_spec: dict[str, list[str]],
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """按 INI 配置按需查询 HTF 指标，返回 {tf: {ind: {field: val}}}。

        Staleness check: 若 HTF 指标的 bar_time 距当前超过 2× HTF 周期，
        视为陈旧数据，跳过注入并记 warning。策略代码无需感知。
        """
        now_utc = datetime.now(timezone.utc)
        result: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for tf, indicator_names in htf_spec.items():
            if tf == current_tf.upper():
                continue
            if tf not in self._configured_timeframes:
                continue
            max_age = timedelta(seconds=timeframe_seconds(tf) * 2)
            tf_indicators: Dict[str, Dict[str, Any]] = {}
            for ind_name in indicator_names:
                ind_data = self._get_htf_indicator(symbol, tf, ind_name)
                if ind_data is None:
                    continue
                # Staleness check via _bar_time metadata
                bar_time_str = ind_data.get("_bar_time")
                if bar_time_str:
                    try:
                        bar_time_val = datetime.fromisoformat(bar_time_str)
                        if bar_time_val.tzinfo is None:
                            bar_time_val = bar_time_val.replace(tzinfo=timezone.utc)
                        age = now_utc - bar_time_val
                        if age > max_age:
                            self._htf_stale_warnings += 1
                            logger.warning(
                                "HTF indicator %s/%s/%s stale: bar_time=%s age=%s > max=%s, skipping (total=%d)",
                                symbol, tf, ind_name, bar_time_str, age, max_age,
                                self._htf_stale_warnings,
                            )
                            continue
                    except (ValueError, TypeError):
                        pass  # parse failure: inject anyway
                tf_indicators[ind_name] = ind_data
            if tf_indicators:
                result[tf] = tf_indicators
        return result

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
        """Quick check: is there ANY strategy that could pass the evaluation loop?

        If min_affinity_skip is disabled (0) or any strategy has sufficient
        affinity, returns True.  Otherwise returns False, allowing the caller
        to skip expensive downstream work (market structure, voting, etc.).
        """
        min_affinity_skip = self.policy.min_affinity_skip
        if min_affinity_skip <= 0.0:
            return True  # affinity gate disabled, always eligible
        strategies = self._target_index.get((symbol, timeframe), [])
        if not strategies:
            return False
        for strategy in strategies:
            allowed_sessions = self.policy.strategy_sessions.get(strategy, ())
            if allowed_sessions and not any(
                s in allowed_sessions for s in active_sessions
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
            affinity = self._effective_affinity(strategy, regime, soft_regime)
            if affinity >= min_affinity_skip:
                return True  # at least one strategy is eligible
        return False

    def _effective_affinity(
        self,
        strategy: str,
        regime: RegimeType,
        soft_regime: Optional[Dict[str, Any]],
        *,
        _parsed_cache: Optional[SoftRegimeResult] = None,
    ) -> float:
        affinity_map = self._strategy_affinity.get(strategy, {})
        if self._soft_regime_enabled and soft_regime:
            try:
                parsed = _parsed_cache or SoftRegimeResult.from_dict(soft_regime)
                return sum(
                    parsed.probability(item) * affinity_map.get(item, 0.5)
                    for item in RegimeType
                )
            except Exception:
                logger.debug(
                    "Failed to parse soft regime for affinity gate: %s",
                    strategy,
                    exc_info=True,
                )
        return affinity_map.get(regime, 0.5)

    def _fuse_vote_decisions(
        self,
        symbol: str,
        timeframe: str,
        bar_time: datetime,
        scope: str,
        decisions: List,
    ) -> List:
        cache_key = (symbol, timeframe, bar_time)
        bucket = self._vote_fusion_cache.setdefault(cache_key, {})
        for decision in decisions:
            existing = bucket.get(decision.strategy)
            if existing is None:
                bucket[decision.strategy] = (scope, decision)
                continue
            existing_scope, existing_decision = existing
            if existing_scope == "intrabar" and scope == "confirmed":
                bucket[decision.strategy] = (scope, decision)
                continue
            if existing_scope == scope:
                bucket[decision.strategy] = (scope, decision)
                continue
            if getattr(decision, "confidence", 0.0) >= getattr(
                existing_decision, "confidence", 0.0
            ):
                bucket[decision.strategy] = (scope, decision)
        self._prune_vote_fusion_cache(symbol, timeframe, bar_time)
        return [item[1] for item in bucket.values()]

    def _prune_vote_fusion_cache(
        self,
        symbol: str,
        timeframe: str,
        current_bar_time: datetime,
    ) -> None:
        keep_after = current_bar_time - timedelta(
            seconds=max(timeframe_seconds(timeframe) * 2, 1)
        )
        stale_keys = [
            key
            for key in self._vote_fusion_cache
            if key[0] == symbol and key[1] == timeframe and key[2] < keep_after
        ]
        for key in stale_keys:
            self._vote_fusion_cache.pop(key, None)
        # 全局容量上限：防止缓存无限积累导致内存泄漏
        _MAX_FUSION_CACHE = 2000
        if len(self._vote_fusion_cache) > _MAX_FUSION_CACHE:
            sorted_keys = sorted(self._vote_fusion_cache.keys(), key=lambda k: k[2])
            to_remove = sorted_keys[: len(sorted_keys) - _MAX_FUSION_CACHE]
            for key in to_remove:
                self._vote_fusion_cache.pop(key, None)

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
