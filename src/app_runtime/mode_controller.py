from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.app_runtime.container import AppContainer

logger = logging.getLogger(__name__)


class RuntimeMode(str, Enum):
    FULL = "full"
    OBSERVE = "observe"
    RISK_OFF = "risk_off"
    INGEST_ONLY = "ingest_only"


class RuntimeModeEODAction(str, Enum):
    DISABLED = "disabled"
    RISK_OFF = "risk_off"
    INGEST_ONLY = "ingest_only"


@dataclass(frozen=True)
class RuntimeModePolicy:
    initial_mode: RuntimeMode = RuntimeMode.FULL
    after_eod_action: RuntimeModeEODAction = RuntimeModeEODAction.DISABLED
    auto_check_interval_seconds: float = 15.0


class RuntimeModeController:
    """统一管理运行模式与组件生命周期。"""

    def __init__(
        self,
        container: AppContainer,
        *,
        policy: RuntimeModePolicy,
    ) -> None:
        self._container = container
        self._policy = policy
        self._current_mode: RuntimeMode | None = None
        self._last_transition_at: datetime | None = None
        self._last_transition_reason: str | None = None
        self._last_error: str | None = None
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._monitor_thread: threading.Thread | None = None

    @property
    def current_mode(self) -> RuntimeMode | None:
        return self._current_mode

    def start(self) -> dict[str, object]:
        snapshot = self.apply_mode(self._policy.initial_mode, reason="startup")
        self._start_monitor()
        return snapshot

    def stop(self) -> None:
        self._stop_event.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=5.0)
            self._monitor_thread = None

    def apply_mode(self, mode: RuntimeMode | str, *, reason: str) -> dict[str, object]:
        target = mode if isinstance(mode, RuntimeMode) else RuntimeMode(str(mode).strip().lower())
        with self._lock:
            self._apply_mode_locked(target, reason=reason)
            return self.snapshot()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "current_mode": self._current_mode.value if self._current_mode else None,
                "configured_mode": self._policy.initial_mode.value,
                "after_eod_action": self._policy.after_eod_action.value,
                "auto_check_interval_seconds": self._policy.auto_check_interval_seconds,
                "last_transition_at": (
                    self._last_transition_at.isoformat() if self._last_transition_at else None
                ),
                "last_transition_reason": self._last_transition_reason,
                "last_error": self._last_error,
                "components": self._component_status_snapshot(),
            }

    def _apply_mode_locked(self, target: RuntimeMode, *, reason: str) -> None:
        if target == RuntimeMode.INGEST_ONLY:
            self._assert_ingest_only_safe()
        registry = self._container.runtime_component_registry
        if registry is None:
            raise RuntimeError("runtime_component_registry is not configured")
        registry.apply_mode(target.value)
        self._current_mode = target
        self._last_transition_at = datetime.now(timezone.utc)
        self._last_transition_reason = reason
        self._last_error = None
        logger.info("Runtime mode applied: mode=%s reason=%s", target.value, reason)

    def _start_monitor(self) -> None:
        if self._policy.after_eod_action == RuntimeModeEODAction.DISABLED:
            return
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return
        self._stop_event.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="runtime-mode-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(timeout=max(1.0, self._policy.auto_check_interval_seconds)):
            try:
                self._evaluate_auto_transition()
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("Runtime mode monitor error: %s", exc, exc_info=True)

    def _evaluate_auto_transition(self) -> None:
        with self._lock:
            if self._current_mode not in {RuntimeMode.FULL, RuntimeMode.OBSERVE}:
                return
            position_manager = self._container.position_manager
            if position_manager is None or not position_manager.is_after_eod_today():
                return
            target = (
                RuntimeMode.RISK_OFF
                if self._policy.after_eod_action == RuntimeModeEODAction.RISK_OFF
                else RuntimeMode.INGEST_ONLY
            )
            if target == RuntimeMode.INGEST_ONLY and not self._can_enter_ingest_only():
                target = RuntimeMode.RISK_OFF
            self._apply_mode_locked(target, reason="after_eod")

    def _assert_ingest_only_safe(self) -> None:
        if not self._can_enter_ingest_only():
            raise RuntimeError("ingest_only requires no open positions and no pending orders")

    def _can_enter_ingest_only(self) -> bool:
        trading = self._container.trade_module
        if trading is None:
            return True
        positions = getattr(trading, "get_positions", None)
        orders = getattr(trading, "get_orders", None)
        try:
            open_positions = list(positions() or []) if callable(positions) else []
            open_orders = list(orders() or []) if callable(orders) else []
        except Exception:
            return False
        return not open_positions and not open_orders

    def _component_status_snapshot(self) -> dict[str, object]:
        registry = self._container.runtime_component_registry
        return registry.status_snapshot() if registry is not None else {}
