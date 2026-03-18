from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Protocol

from src.utils.common import timeframe_seconds

from .filters import SignalFilterChain
from .models import SignalEvent
from .policy import RuntimeSignalState, SignalPolicy
from .service import SignalModule

if TYPE_CHECKING:
    from src.indicators.manager import UnifiedIndicatorManager

logger = logging.getLogger(__name__)


class SnapshotSource(Protocol):
    def add_snapshot_listener(
        self,
        listener: Callable[[str, str, datetime, Dict[str, Dict[str, float]], str], None],
    ) -> None:
        ...

    def remove_snapshot_listener(
        self,
        listener: Callable[[str, str, datetime, Dict[str, Dict[str, float]], str], None],
    ) -> None:
        ...


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
    ):
        self.service = service
        self.snapshot_source = snapshot_source
        self.enable_confirmed_snapshot = bool(enable_confirmed_snapshot)
        self.enable_intrabar = bool(enable_intrabar)
        self.policy = policy or SignalPolicy()
        self.filter_chain = filter_chain
        self._signal_listeners: List[Callable[[SignalEvent], None]] = []
        self._targets = list(targets)
        self._target_index: dict[tuple[str, str], list[str]] = {}
        self._strategy_requirements: dict[str, tuple[str, ...]] = {}
        # Maps strategy name → frozenset of scopes it wants to receive.
        # Populated from strategy_impl.preferred_scopes; falls back to both
        # scopes for strategies that do not declare a preference.
        self._strategy_scopes: dict[str, frozenset[str]] = {}
        requirements_getter = getattr(self.service, "strategy_requirements", None)
        scopes_getter = getattr(self.service, "strategy_scopes", None)
        for target in self._targets:
            self._target_index.setdefault((target.symbol, target.timeframe), []).append(target.strategy)
            if target.strategy not in self._strategy_requirements:
                if callable(requirements_getter):
                    self._strategy_requirements[target.strategy] = tuple(requirements_getter(target.strategy))
                else:
                    self._strategy_requirements[target.strategy] = ()
            if target.strategy not in self._strategy_scopes:
                if callable(scopes_getter):
                    try:
                        self._strategy_scopes[target.strategy] = frozenset(scopes_getter(target.strategy))
                    except Exception:
                        self._strategy_scopes[target.strategy] = frozenset(("intrabar", "confirmed"))
                else:
                    self._strategy_scopes[target.strategy] = frozenset(("intrabar", "confirmed"))

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._events: queue.Queue = queue.Queue(maxsize=4096)
        self._last_run_at: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._run_count = 0
        self._processed_events = 0
        self._dropped_events = 0
        self._last_drop_log_at: float = 0.0
        self._state_by_target: dict[tuple[str, str, str], RuntimeSignalState] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._restore_state()
        self.snapshot_source.add_snapshot_listener(self._on_snapshot)
        self._thread = threading.Thread(target=self._loop, name="signal-runtime", daemon=True)
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
        }
        if scope == "intrabar":
            if bar_time.tzinfo is None:
                bar_time = bar_time.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - bar_time.astimezone(timezone.utc)).total_seconds()
            metadata["bar_progress"] = max(0.0, min(elapsed / max(timeframe_seconds(timeframe), 1), 1.0))
        self._enqueue((scope, symbol, timeframe, indicators, metadata))

    def _enqueue(
        self,
        item: tuple[str, str, str, Dict[str, Dict[str, float]], Dict[str, Any]],
    ) -> None:
        try:
            self._events.put_nowait(item)
        except queue.Full:
            self._dropped_events += 1
            now = time.monotonic()
            # Rate-limit error logs to at most once per 60 s to avoid log spam.
            if now - self._last_drop_log_at >= 60.0:
                self._last_drop_log_at = now
                scope, symbol, timeframe = item[0], item[1], item[2]
                logger.error(
                    "Signal runtime queue is full — indicator snapshot dropped "
                    "(total_dropped=%d, scope=%s, symbol=%s, timeframe=%s). "
                    "Consider increasing maxsize=4096 or reducing intrabar frequency.",
                    self._dropped_events,
                    scope,
                    symbol,
                    timeframe,
                )

    def status(self) -> dict:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "target_count": len(self._targets),
            "trigger_mode": {
                "confirmed_snapshot": self.enable_confirmed_snapshot,
                "intrabar": self.enable_intrabar,
            },
            "strategy_scopes": {
                name: sorted(scopes)
                for name, scopes in self._strategy_scopes.items()
            },
            "run_count": self._run_count,
            "processed_events": self._processed_events,
            "dropped_events": self._dropped_events,
            "queue_size": self._events.qsize(),
            "queue_capacity": self._events.maxsize,
            "last_run_at": self._last_run_at.isoformat() if self._last_run_at else None,
            "last_error": self._last_error,
            "active_preview_states": sum(
                1 for state in self._state_by_target.values() if state.preview_state != "idle"
            ),
            "active_confirmed_states": sum(
                1 for state in self._state_by_target.values() if state.confirmed_state != "idle"
            ),
        }

    def add_signal_listener(self, listener: Callable[[SignalEvent], None]) -> None:
        """Register a callback to receive SignalEvent on every state transition."""
        if listener not in self._signal_listeners:
            self._signal_listeners.append(listener)

    def remove_signal_listener(self, listener: Callable[[SignalEvent], None]) -> None:
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
        if not self._signal_listeners:
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
        for listener in list(self._signal_listeners):
            try:
                listener(event)
            except Exception as exc:
                self._last_error = f"Signal listener error: {exc}"
                logger.warning("Signal listener error (%s): %s", listener, exc)

    @staticmethod
    def _parse_event_time(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        text = str(value)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

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
            scope = str(row.get("scope") or metadata.get("scope") or "confirmed").strip().lower()
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
                self._restore_confirmed_state(state, signal_state, generated_at, bar_time)
                continue

            if scope in {"preview", "intrabar"} and key not in restored_preview:
                # Don't restore a preview state that pre-dates an already-
                # restored confirmed event: the confirmed state supersedes it.
                confirmed_at = restored_confirmed_at.get(key)
                if confirmed_at is not None and generated_at <= confirmed_at:
                    continue
                restored_preview.add(key)
                self._restore_preview_state(key[1], state, signal_state, generated_at, bar_time, now)

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
        if signal_state not in {"preview_buy", "preview_sell", "armed_buy", "armed_sell"}:
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
    def _snapshot_signature(indicators: Dict[str, Dict[str, float]]) -> tuple:
        normalized = []
        for indicator_name in sorted(indicators.keys()):
            payload = indicators.get(indicator_name) or {}
            normalized_payload = tuple(
                (metric_name, payload[metric_name])
                for metric_name in sorted(payload.keys())
            )
            normalized.append((indicator_name, normalized_payload))
        return tuple(normalized)

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
        if state.last_snapshot_scope == scope and state.last_snapshot_bar_time == bar_time and state.last_snapshot_signature == signature:
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
                return self._build_transition_metadata(
                    metadata,
                    signal_state=signal_state,
                    state_changed=True,
                    previous_state=previous_state,
                )
            state.confirmed_state = "idle"
            state.confirmed_bar_time = bar_time
            return None

        signal_state = f"confirmed_{decision_action}"
        state.confirmed_state = signal_state
        state.confirmed_bar_time = bar_time
        if not self._should_emit(state, signal_state, event_time, bar_time, cooldown_seconds=0.0):
            return None
        self._mark_emitted(state, signal_state, event_time, bar_time)
        return self._build_transition_metadata(
            metadata,
            signal_state=signal_state,
            state_changed=signal_state != previous_state,
            previous_state=previous_state,
        )

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

    def process_next_event(self, timeout: float = 0.5) -> bool:
        try:
            event = self._events.get(timeout=timeout)
        except queue.Empty:
            return False
        scope, symbol, timeframe, indicators, metadata = event
        event_time = self._parse_event_time(metadata.get("snapshot_time", datetime.now(timezone.utc)))
        bar_time = self._parse_event_time(metadata.get("bar_time", event_time))

        if self.filter_chain is not None:
            spread_points = float(metadata.get("spread_points", 0.0))
            allowed, reason = self.filter_chain.should_evaluate(
                symbol, spread_points=spread_points, utc_now=event_time,
            )
            if not allowed:
                logger.debug("Signal evaluation skipped for %s/%s: %s", symbol, timeframe, reason)
                self._processed_events += 1
                self._run_count += 1
                self._last_run_at = datetime.now(timezone.utc)
                return True

        strategies = self._target_index.get((symbol, timeframe), [])
        for strategy in strategies:
            # Skip strategies that do not want this scope.
            allowed_scopes = self._strategy_scopes.get(strategy, frozenset(("intrabar", "confirmed")))
            if scope not in allowed_scopes:
                continue
            required_indicators = self._strategy_requirements.get(strategy, ())
            if required_indicators:
                if any(indicator_name not in indicators for indicator_name in required_indicators):
                    continue
                scoped_indicators = {
                    indicator_name: indicators[indicator_name]
                    for indicator_name in required_indicators
                }
            else:
                scoped_indicators = indicators
            state = self._state_by_target.setdefault(
                (symbol, timeframe, strategy),
                RuntimeSignalState(),
            )
            if not self._should_evaluate_snapshot(
                state,
                scope=scope,
                event_time=event_time,
                bar_time=bar_time,
                indicators=scoped_indicators,
            ):
                continue
            decision = self.service.evaluate(
                symbol=symbol,
                timeframe=timeframe,
                strategy=strategy,
                indicators=scoped_indicators,
                metadata=metadata,
                persist=False,
            )
            transition_metadata = (
                self._transition_confirmed(state, decision.action, event_time, bar_time, metadata)
                if scope == "confirmed"
                else self._transition_preview(state, decision.action, decision.confidence, event_time, bar_time, metadata)
            )
            if transition_metadata is None:
                continue
            record = self.service.persist_decision(
                decision,
                indicators=scoped_indicators,
                metadata=transition_metadata,
            )
            signal_id = record.signal_id if record is not None else ""
            self._publish_signal_event(decision, signal_id, scope, scoped_indicators, transition_metadata)
        self._processed_events += 1
        self._run_count += 1
        self._last_run_at = datetime.now(timezone.utc)
        self._last_error = None
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.process_next_event(timeout=0.5)
            except Exception as exc:
                self._last_error = str(exc)
                logger.exception("Signal runtime event processing failed: %s", exc)
