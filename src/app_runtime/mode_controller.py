from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.app_runtime.mode_policy import (
    RuntimeMode,
    RuntimeModeAutoTransitionPolicy,
    RuntimeModePolicy,
    RuntimeModeTransitionGuard,
)

if TYPE_CHECKING:
    from src.app_runtime.container import AppContainer

logger = logging.getLogger(__name__)


class RuntimeModeController:
    """统一管理运行模式状态与组件生命周期执行。"""

    def __init__(
        self,
        container: AppContainer,
        *,
        policy: RuntimeModePolicy,
        guard: RuntimeModeTransitionGuard,
        auto_transition_policy: RuntimeModeAutoTransitionPolicy,
    ) -> None:
        self._container = container
        self._policy = policy
        self._guard = guard
        self._auto_transition_policy = auto_transition_policy
        self._current_mode: RuntimeMode | None = None
        self._is_auto_transitioned: bool = False
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
            self._is_auto_transitioned = False
            self._apply_mode_locked(target, reason=reason)
            return self.snapshot()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "current_mode": self._current_mode.value if self._current_mode else None,
                "configured_mode": self._policy.initial_mode.value,
                "after_eod_action": self._policy.after_eod_action.value,
                "auto_check_interval_seconds": self._policy.auto_check_interval_seconds,
                "is_auto_transitioned": self._is_auto_transitioned,
                "last_transition_at": (
                    self._last_transition_at.isoformat() if self._last_transition_at else None
                ),
                "last_transition_reason": self._last_transition_reason,
                "last_error": self._last_error,
                "components": self._component_status_snapshot(),
            }

    def _apply_mode_locked(self, target: RuntimeMode, *, reason: str) -> None:
        self._guard.ensure_can_enter(target)
        registry = self._container.runtime_component_registry
        if registry is None:
            raise RuntimeError("runtime_component_registry is not configured")
        partial_error: str | None = None
        try:
            registry.apply_mode(target.value)
        except RuntimeError as exc:
            # 部分组件失败，但模式已尽力切换。记录错误，继续更新状态。
            partial_error = str(exc)
            logger.warning(
                "Mode '%s' applied with partial failure: %s", target.value, exc
            )
        self._current_mode = target
        self._last_transition_at = datetime.now(timezone.utc)
        self._last_transition_reason = reason
        self._last_error = partial_error
        if partial_error is None:
            logger.info("Runtime mode applied: mode=%s reason=%s", target.value, reason)

    def _start_monitor(self) -> None:
        if not self._auto_transition_policy.enabled:
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
            position_manager = self._container.position_manager
            after_eod_today = bool(
                position_manager is not None and position_manager.is_after_eod_today()
            )

            # 新交易日检测：EOD 自动降级后恢复初始模式
            restore = self._auto_transition_policy.resolve_session_start(
                current_mode=self._current_mode,
                after_eod_today=after_eod_today,
                was_auto_transitioned=self._is_auto_transitioned,
                initial_mode=self._policy.initial_mode,
            )
            if restore is not None:
                self._is_auto_transitioned = False
                self._apply_mode_locked(restore, reason="session_start")
                return

            # EOD 自动降级
            target = self._auto_transition_policy.resolve_after_eod(
                current_mode=self._current_mode,
                after_eod_today=after_eod_today,
                can_enter_ingest_only=self._guard.can_enter_ingest_only(),
            )
            if target is None:
                return
            self._is_auto_transitioned = True
            self._apply_mode_locked(target, reason="after_eod")

    def _component_status_snapshot(self) -> dict[str, object]:
        registry = self._container.runtime_component_registry
        return registry.status_snapshot() if registry is not None else {}
