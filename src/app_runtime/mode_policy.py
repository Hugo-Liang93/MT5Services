from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


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
    trading_module_getter: Callable[[], Any | None]

    def can_enter(self, mode: RuntimeMode) -> bool:
        if mode != RuntimeMode.INGEST_ONLY:
            return True
        return self.can_enter_ingest_only()

    def ensure_can_enter(self, mode: RuntimeMode) -> None:
        if not self.can_enter(mode):
            raise RuntimeError("ingest_only requires no open positions and no pending orders")

    def can_enter_ingest_only(self) -> bool:
        trading = self.trading_module_getter()
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
