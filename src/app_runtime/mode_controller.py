from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Optional

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
    """统一管理运行模式与模块生命周期。"""

    def __init__(
        self,
        container: AppContainer,
        *,
        policy: RuntimeModePolicy,
        signal_config_loader: Callable[[], Any] | None = None,
    ) -> None:
        self._container = container
        self._policy = policy
        self._signal_config_loader = signal_config_loader
        self._current_mode: RuntimeMode | None = None
        self._last_transition_at: datetime | None = None
        self._last_transition_reason: str | None = None
        self._last_error: str | None = None
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._trade_listener_attached = False

    @property
    def current_mode(self) -> RuntimeMode | None:
        return self._current_mode

    def start(self) -> dict[str, Any]:
        snapshot = self.apply_mode(
            self._policy.initial_mode,
            reason="startup",
        )
        self._start_monitor()
        return snapshot

    def stop(self) -> None:
        self._stop_event.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=5.0)
            self._monitor_thread = None

    def apply_mode(self, mode: RuntimeMode | str, *, reason: str) -> dict[str, Any]:
        target = mode if isinstance(mode, RuntimeMode) else RuntimeMode(str(mode).strip().lower())
        with self._lock:
            self._apply_mode_locked(target, reason=reason)
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
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

        self._start_storage()
        self._start_ingestion()

        if target in {RuntimeMode.FULL, RuntimeMode.OBSERVE}:
            self._start_indicator_stack()
            self._start_signal_stack()
            self._set_trade_listener(attached=(target == RuntimeMode.FULL))
            self._start_position_stack()
        elif target == RuntimeMode.RISK_OFF:
            self._set_trade_listener(attached=False)
            self._stop_signal_stack()
            self._stop_indicator_stack()
            self._start_position_stack()
        elif target == RuntimeMode.INGEST_ONLY:
            self._set_trade_listener(attached=False)
            self._stop_signal_stack()
            self._stop_indicator_stack()
            self._stop_position_stack()

        self._current_mode = target
        self._last_transition_at = datetime.now(timezone.utc)
        self._last_transition_reason = reason
        self._last_error = None
        logger.info("Runtime mode applied: mode=%s reason=%s", target.value, reason)

    def _start_storage(self) -> None:
        storage = self._container.storage_writer
        if storage is not None:
            storage.start()

    def _start_ingestion(self) -> None:
        ingestor = self._container.ingestor
        if ingestor is not None:
            ingestor.start()

    def _start_indicator_stack(self) -> None:
        indicators = self._container.indicator_manager
        if indicators is not None:
            indicators.start()
        self._start_calibrator()
        calendar = self._container.economic_calendar_service
        if calendar is not None:
            calendar.start()

    def _stop_indicator_stack(self) -> None:
        self._stop_calibrator()
        calendar = self._container.economic_calendar_service
        if calendar is not None:
            calendar.stop()
        indicators = self._container.indicator_manager
        if indicators is not None:
            indicators.shutdown()

    def _start_signal_stack(self) -> None:
        runtime = self._container.signal_runtime
        if runtime is not None:
            runtime.start()

    def _stop_signal_stack(self) -> None:
        runtime = self._container.signal_runtime
        if runtime is not None:
            runtime.stop()
        executor = self._container.trade_executor
        if executor is not None:
            executor.shutdown()

    def _start_position_stack(self) -> None:
        pending = self._container.pending_entry_manager
        if pending is not None:
            pending_was_running = self._thread_alive(pending, "_monitor_thread")
            pending.start()
            recovery = self._container.trading_state_recovery
            trading = self._container.trade_module
            if not pending_was_running and recovery is not None and trading is not None:
                try:
                    result = recovery.restore_pending_orders(
                        pending_entry_manager=pending,
                        trading_module=trading,
                    )
                    logger.info("Pending order recovery: %s", result)
                except Exception:
                    logger.warning("Pending order restore failed", exc_info=True)

        position_manager = self._container.position_manager
        if position_manager is not None:
            position_was_running = self._thread_alive(position_manager, "_reconcile_thread")
            position_manager.start(reconcile_interval=self._position_reconcile_interval())
            if not position_was_running:
                try:
                    recovery_result = position_manager.sync_open_positions()
                    logger.info("PositionManager mode sync: %s", recovery_result)
                except Exception:
                    logger.warning("PositionManager mode sync failed", exc_info=True)
                try:
                    overnight_result = position_manager.force_close_overnight()
                    if overnight_result is not None:
                        logger.warning("Overnight force close on mode start: %s", overnight_result)
                except Exception:
                    logger.warning("Overnight force close failed", exc_info=True)

    def _stop_position_stack(self) -> None:
        pending = self._container.pending_entry_manager
        if pending is not None:
            pending.shutdown()

        position_manager = self._container.position_manager
        if position_manager is not None:
            position_manager.stop()

    def _set_trade_listener(self, *, attached: bool) -> None:
        runtime = self._container.signal_runtime
        executor = self._container.trade_executor
        if runtime is None or executor is None:
            return
        if attached and not self._trade_listener_attached:
            runtime.add_signal_listener(executor.on_signal_event)
            self._trade_listener_attached = True
        elif not attached and self._trade_listener_attached:
            runtime.remove_signal_listener(executor.on_signal_event)
            self._trade_listener_attached = False

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

    def _position_reconcile_interval(self) -> float:
        if self._signal_config_loader is None:
            return 60.0
        try:
            return float(self._signal_config_loader().position_reconcile_interval)
        except Exception:
            return 60.0

    def _start_calibrator(self) -> None:
        calibrator = self._container.calibrator
        if calibrator is None:
            return
        try:
            from src.config import get_runtime_data_path

            calibrator_cache_path = get_runtime_data_path("calibrator_cache.json")
            os.makedirs(os.path.dirname(calibrator_cache_path), exist_ok=True)
            calibrator.load(calibrator_cache_path)
        except Exception:
            logger.debug("Calibrator warm-start load failed", exc_info=True)
        try:
            calibrator.start_background_refresh()
        except Exception:
            logger.debug("Calibrator start failed", exc_info=True)

    def _stop_calibrator(self) -> None:
        calibrator = self._container.calibrator
        if calibrator is None:
            return
        try:
            calibrator.stop_background_refresh()
        except Exception:
            logger.debug("Calibrator stop failed", exc_info=True)

    def _component_status_snapshot(self) -> dict[str, Any]:
        return {
            "storage": self._thread_alive(self._container.storage_writer, "_thread"),
            "ingestion": self._thread_alive(self._container.ingestor, "_thread"),
            "indicators": self._thread_alive(self._container.indicator_manager, "_event_thread"),
            "signals": self._thread_alive(self._container.signal_runtime, "_thread"),
            "trade_listener_attached": self._trade_listener_attached,
            "pending_entry": self._thread_alive(
                self._container.pending_entry_manager,
                "_monitor_thread",
            ),
            "position_manager": self._thread_alive(
                self._container.position_manager,
                "_reconcile_thread",
            ),
        }

    @staticmethod
    def _thread_alive(component: Any, attr: str) -> bool:
        if component is None:
            return False
        thread = getattr(component, attr, None)
        return bool(thread is not None and thread.is_alive())
