from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from src.trading.ports import TradingQueryPort


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


@dataclass(frozen=True)
class RuntimeModeTransitionGuard:
    trading_module_getter: Callable[[], TradingQueryPort | None]

    def can_enter(self, mode: RuntimeMode) -> bool:
        if mode != RuntimeMode.INGEST_ONLY:
            return True
        return self.can_enter_ingest_only()

    def ensure_can_enter(self, mode: RuntimeMode) -> None:
        if not self.can_enter(mode):
            raise RuntimeError(
                "ingest_only requires no open positions and no pending orders"
            )

    def can_enter_ingest_only(self) -> bool:
        trading = self.trading_module_getter()
        if trading is None:
            return True
        try:
            open_positions = list(trading.get_positions() or [])
            open_orders = list(trading.get_orders() or [])
        except Exception:
            return False
        return not open_positions and not open_orders


@dataclass(frozen=True)
class RuntimeModeAutoTransitionPolicy:
    after_eod_action: RuntimeModeEODAction = RuntimeModeEODAction.DISABLED

    @property
    def enabled(self) -> bool:
        return self.after_eod_action != RuntimeModeEODAction.DISABLED

    def resolve_after_eod(
        self,
        *,
        current_mode: RuntimeMode | None,
        after_eod_today: bool,
        can_enter_ingest_only: bool,
    ) -> RuntimeMode | None:
        if current_mode not in {RuntimeMode.FULL, RuntimeMode.OBSERVE}:
            return None
        if not after_eod_today:
            return None
        target = (
            RuntimeMode.RISK_OFF
            if self.after_eod_action == RuntimeModeEODAction.RISK_OFF
            else RuntimeMode.INGEST_ONLY
        )
        if target == RuntimeMode.INGEST_ONLY and not can_enter_ingest_only:
            return RuntimeMode.RISK_OFF
        return target

    def resolve_session_start(
        self,
        *,
        current_mode: RuntimeMode | None,
        after_eod_today: bool,
        was_auto_transitioned: bool,
        initial_mode: RuntimeMode,
    ) -> RuntimeMode | None:
        """新交易日开始：若当前模式是 EOD 自动降级的结果，恢复到初始模式。"""
        if not was_auto_transitioned:
            return None
        if after_eod_today:
            return None
        if current_mode == initial_mode:
            return None
        return initial_mode
