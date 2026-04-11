"""SignalRuntime 的职责分离组件。"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from time import monotonic
from typing import Any, TYPE_CHECKING

from ..service import StrategyCapability
from .runtime_processing import dequeue_event as _runtime_dequeue_event
from .runtime_processing import enqueue_event as _runtime_enqueue_event
from .runtime_processing import on_snapshot as _runtime_on_snapshot
from .runtime_status import (
    build_runtime_status,
    compute_filter_window_stats,
    count_active_states,
    describe_voting,
    get_regime_stability,
    get_regime_stability_map,
    get_voting_info,
    voting_groups_summary,
)
from .runtime_warmup import check_warmup_barrier
from .runtime_metadata import build_snapshot_metadata
from .state_machine import (
    build_transition_metadata as _build_transition_metadata,
)
from .state_machine import is_new_snapshot as _sm_is_new_snapshot
from .state_machine import mark_emitted as _sm_mark_emitted
from .state_machine import should_emit as _sm_should_emit
from .state_machine import snapshot_signature as _sm_snapshot_signature
from .state_machine import transition_confirmed
from .policy import SignalPolicy
from .voting import StrategyVotingEngine

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .runtime import SignalRuntime


class SignalLifecyclePolicy:
    """状态机与生命周期更新策略的薄包装层。"""

    def __init__(self, runtime: "SignalRuntime"):
        self._runtime = runtime

    @staticmethod
    def parse_event_time(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        text = str(value)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def build_transition_metadata(
        metadata: dict[str, Any],
        *,
        signal_state: str,
        state_changed: bool,
        previous_state: str,
    ) -> dict[str, Any]:
        return _build_transition_metadata(
            metadata,
            signal_state=signal_state,
            state_changed=state_changed,
            previous_state=previous_state,
        )

    def restore_state(self) -> None:
        # 统一状态恢复入口：复用 runtime 恢复函数。
        from . import runtime_recovery as _runtime_recovery

        _runtime_recovery.restore_state(self._runtime)

    def restore_confirmed_state(
        self,
        state: Any,
        signal_state: str,
        generated_at: datetime,
        bar_time: datetime,
    ) -> None:
        from . import runtime_recovery as _runtime_recovery

        _runtime_recovery.restore_confirmed_state(
            state, signal_state, generated_at, bar_time
        )

    @staticmethod
    def should_emit(
        state: Any,
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
    def mark_emitted(
        state: Any,
        signal_state: str,
        event_time: datetime,
        bar_time: datetime,
    ) -> None:
        _sm_mark_emitted(state, signal_state, event_time, bar_time)

    @staticmethod
    def snapshot_signature(indicators: dict[str, dict[str, float]]) -> int:
        return _sm_snapshot_signature(indicators)

    def is_new_snapshot(
        self,
        state: Any,
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
            dedupe_window_seconds=self._runtime.policy.snapshot_dedupe_window_seconds,
        )

    @staticmethod
    def transition_confirmed(
        state: Any,
        decision_action: str,
        event_time: datetime,
        bar_time: datetime,
        metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        return transition_confirmed(
            state, decision_action, event_time, bar_time, metadata
        )

    def all_intrabar_strategies_satisfied(
        self,
        indicators: dict[str, dict[str, float]],
    ) -> bool:
        if not indicators:
            return False
        indicator_names = set(indicators.keys())
        for strategy in self._runtime.policy.intrabar_strategies():
            capability = self._runtime.policy.get_strategy_capability(strategy)
            if capability is None:
                continue
            required = capability.needed_indicators
            if required and any(ind not in indicator_names for ind in required):
                return False
        return True

    def get_shard_lock(self, symbol: str, timeframe: str):
        key = (symbol, timeframe)
        lock = self._runtime._shard_locks.get(key)
        if lock is not None:
            return lock
        with self._runtime._meta_lock:
            return self._runtime._shard_locks.setdefault(key, threading.Lock())

    def get_regime_stability(self, symbol: str, timeframe: str) -> dict[str, Any] | None:
        return get_regime_stability(self._runtime, symbol, timeframe)

    def get_regime_stability_map(self) -> dict[str, dict[str, Any]]:
        return get_regime_stability_map(self._runtime)


class RuntimeStatusBuilder:
    """状态快照构造集中口。"""

    def __init__(self, runtime: "SignalRuntime"):
        self._runtime = runtime

    def status(self) -> dict:
        return build_runtime_status(self._runtime)

    def describe_voting(self) -> list[dict[str, Any]]:
        return describe_voting(self._runtime)

    def voting_groups_summary(self) -> list[dict[str, Any]]:
        return voting_groups_summary(self._runtime)

    def compute_filter_window_stats(self) -> dict:
        return compute_filter_window_stats(self._runtime)

    def count_active_states(self) -> dict:
        return count_active_states(self._runtime)

    def get_voting_info(self) -> dict[str, Any]:
        return get_voting_info(self._runtime)

    def get_regime_stability(self, symbol: str, timeframe: str) -> dict[str, Any] | None:
        return get_regime_stability(self._runtime, symbol, timeframe)

    def get_regime_stability_map(self) -> dict[str, dict[str, Any]]:
        return get_regime_stability_map(self._runtime)

    def filter_status(self) -> dict[str, Any]:
        return self._runtime.filter_chain.filter_status() if self._runtime.filter_chain is not None else {}


class RuntimePolicyCoordinator:
    """运行时策略能力、voting 引擎与目标映射同步入口。"""

    def __init__(self, runtime: "SignalRuntime"):
        self._runtime = runtime

    @staticmethod
    def _normalize_timeframe(timeframe: str) -> str:
        return str(timeframe).strip().upper()

    @staticmethod
    def _build_group_engines(
        policy: SignalPolicy,
    ) -> "list[tuple[Any, StrategyVotingEngine]]":
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

    @staticmethod
    def _build_voting_engine(policy: SignalPolicy) -> StrategyVotingEngine | None:
        if policy.voting_enabled and not policy.voting_groups:
            return StrategyVotingEngine(
                consensus_threshold=policy.voting_consensus_threshold,
                min_quorum=policy.voting_min_quorum,
                disagreement_penalty=policy.voting_disagreement_penalty,
            )
        return None

    @staticmethod
    def _build_group_members(policy: SignalPolicy) -> frozenset[str]:
        return frozenset(
            name for group in policy.voting_groups for name in group.strategies
        ) - policy.standalone_override

    def _resolve_strategy_capabilities(
        self,
        group_policy: dict[str, str],
    ) -> tuple[StrategyCapability, ...] | list[dict[str, Any]] | None:
        raw_catalog = self._runtime.service.strategy_capability_catalog(
            voting_group_policy=group_policy
        )
        if raw_catalog is None:
            return None
        if isinstance(raw_catalog, dict):
            raw_items = [raw_catalog]
        else:
            try:
                raw_items = list(raw_catalog)
            except TypeError:
                raw_items = [raw_catalog]
        if not raw_items:
            return None
        return tuple(raw_items)

    def _refresh_strategy_capabilities(self) -> None:
        group_policy = {
            strategy_name: group.name
            for group in self._runtime.policy.voting_groups
            for strategy_name in group.strategies
        }
        raw_catalog = self._resolve_strategy_capabilities(group_policy)
        if raw_catalog is None:
            raw_catalog = ()
        if not self._runtime._targets and not raw_catalog:
            return
        capability_contract: list[dict[str, Any]] = []
        invalid_item_types: list[str] = []
        for item in raw_catalog:
            if isinstance(item, StrategyCapability):
                capability_contract.append(item.as_contract())
                continue
            if isinstance(item, dict):
                capability_contract.append(dict(item))
                continue
            invalid_item_types.append(type(item).__name__)
        if invalid_item_types:
            raise TypeError(
                "SignalRuntime capability catalog item must be StrategyCapability or dict: "
                + ", ".join(sorted(set(invalid_item_types)))
            )
        if not capability_contract:
            raise ValueError(
                "SignalRuntime requires non-empty strategy capability catalog from SignalModule"
            )
        self._runtime.policy.set_strategy_capability_contract(capability_contract)

    def _build_target_index(self) -> dict[tuple[str, str], list[str]]:
        index: dict[tuple[str, str], list[str]] = {}
        dropped_targets = 0
        dropped_samples: list[str] = []
        seen: set[tuple[str, str, str]] = set()
        for target in self._runtime._targets:
            symbol = str(target.symbol)
            timeframe = self._normalize_timeframe(target.timeframe)
            strategy = str(target.strategy)
            capability = self._runtime.policy.get_strategy_capability(strategy)
            if capability is None:
                raise ValueError(f"unsupported signal strategy: {strategy}")

            allowed_timeframes = self._runtime.policy.strategy_timeframes.get(strategy, ())
            if allowed_timeframes:
                allowed_set = {
                    self._normalize_timeframe(tf) for tf in allowed_timeframes
                }
                if timeframe not in allowed_set:
                    dropped_targets += 1
                    if len(dropped_samples) < 20:
                        dropped_samples.append(f"{symbol}/{timeframe}:{strategy}")
                    continue

            key = (symbol, timeframe)
            seen_key = (symbol, timeframe, strategy)
            if seen_key in seen:
                continue
            seen.add(seen_key)
            index.setdefault(key, []).append(strategy)

        if dropped_targets:
            logger.info(
                "SignalRuntime: filtered %d target entries by strategy_timeframes in prefilter",
                dropped_targets,
            )
            if dropped_samples:
                logger.debug("SignalRuntime prefilter samples: %s", dropped_samples)

        return index

    def apply(self, policy: SignalPolicy) -> None:
        self._runtime.policy = policy
        self._runtime._voting_engine = self._build_voting_engine(policy)
        self._runtime._voting_group_engines = self._build_group_engines(policy)
        self._runtime._voting_group_members = self._build_group_members(policy)
        self._refresh_strategy_capabilities()
        self._runtime._target_index = self._build_target_index()


class RuntimeLifecycleManager:
    """运行时生命周期管理：线程回收、启动状态复位与生命周期收口。"""

    def __init__(self, runtime: "SignalRuntime"):
        self._runtime = runtime

    @staticmethod
    def _clear_previous_listener(runtime: "SignalRuntime") -> None:
        try:
            runtime._queue_runner.detach()
        except Exception:
            logger.debug(
                "SignalRuntime: failed to detach stale snapshot listener",
                exc_info=True,
            )

    def wait_previous_loop(self, timeout: float = 5.0) -> bool:
        old = self._runtime._thread
        if old is None:
            return True
        if not old.is_alive():
            self._runtime._thread = None
            return True

        logger.warning("SignalRuntime: waiting for previous thread to finish")
        old.join(timeout=timeout)
        if old.is_alive():
            logger.error("SignalRuntime: previous thread still alive after re-join")
            return False
        self._runtime._thread = None
        return True

    def prepare_startup(self) -> None:
        self._clear_previous_listener(self._runtime)
        self._runtime._stop.clear()
        if self._runtime._wal_db_path:
            self._runtime._confirmed_events.reopen()
        self._runtime._restore_state()
        self.reset_observability()

    def reset_observability(self) -> None:
        runtime = self._runtime
        runtime._dropped_events = 0
        runtime._run_count = 0
        runtime._processed_events = 0
        runtime._dropped_confirmed = 0
        runtime._dropped_intrabar = 0
        runtime._intrabar_stale_drops = 0
        runtime._intrabar_overflow_wait_success = 0
        runtime._intrabar_overflow_replace_success = 0
        runtime._intrabar_overflow_failures = 0
        runtime._confirmed_backpressure_waits = 0
        runtime._confirmed_backpressure_failures = 0
        runtime._dropped_at_last_log = 0
        runtime._last_drop_log_at = 0.0
        runtime._filter_window = deque()
        runtime._filter_started_at = monotonic()
        runtime._vote_fusion_cache = {}
        runtime._voting_stats = runtime._empty_voting_stats(
            bool(runtime._voting_group_engines)
        )

    def start_loop(self, target, *, name: str = "signal-runtime") -> None:
        self._runtime._queue_runner.attach()
        self._runtime._thread = threading.Thread(
            target=target, name=name, daemon=True
        )
        self._runtime._thread.start()

    def stop_loop(self, timeout: float) -> bool:
        self._runtime._stop.set()
        self._runtime._queue_runner.detach()
        thread = self._runtime._thread
        if thread is None:
            return True

        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning(
                "SignalRuntime thread did not stop within %.1fs, "
                "will be cleaned up on next start()",
                timeout,
            )
            return False

        self._runtime._thread = None
        return True


class QueueRunner:
    """事件入队/出队与 snapshot 监听生命周期。"""

    def __init__(self, runtime: "SignalRuntime"):
        self._runtime = runtime
        self._snapshot_listener = self._build_snapshot_listener()

    def _build_snapshot_listener(self):
        def _on_snapshot(
            symbol: str,
            timeframe: str,
            bar_time: datetime,
            indicators: dict[str, dict[str, float]],
            scope: str,
        ) -> None:
            item = _runtime_on_snapshot(
                self._runtime,
                symbol,
                timeframe,
                bar_time,
                indicators,
                scope,
                check_warmup_barrier,
                build_snapshot_metadata,
            )
            if item is None:
                return
            self.enqueue(item)

        return _on_snapshot

    def on_snapshot(
        self,
        symbol: str,
        timeframe: str,
        bar_time: datetime,
        indicators: dict[str, dict[str, float]],
        scope: str,
    ) -> None:
        self._snapshot_listener(symbol, timeframe, bar_time, indicators, scope)

    def attach(self) -> None:
        self._runtime.snapshot_source.add_snapshot_listener(self._snapshot_listener)

    def detach(self) -> None:
        self._runtime.snapshot_source.remove_snapshot_listener(self._snapshot_listener)

    def enqueue(self, item: tuple[str, str, str, dict[str, dict[str, float]], dict[str, Any]]) -> None:
        _runtime_enqueue_event(self._runtime, item)

    def dequeue(self, timeout: float) -> tuple | None:
        return _runtime_dequeue_event(self._runtime, timeout)

    @staticmethod
    def _clear_queue(q) -> None:
        import queue

        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                break

    def clear_queues(self) -> None:
        self._clear_queue(self._runtime._confirmed_events)
        self._clear_queue(self._runtime._intrabar_events)
