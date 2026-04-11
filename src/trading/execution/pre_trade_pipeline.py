"""Trade execution 预交易流水线组件（门面层职责收敛）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.signals.metadata_keys import MetadataKey as MK
from .reasons import (
    REASON_INTRABAR_GUARD_MISSING,
    REASON_INTRABAR_PARENT_BAR_MISSING,
    REASON_INTRABAR_GATE_BLOCKED,
    REASON_INTRABAR_GUARD_BLOCKED,
)
from . import pre_trade_checks as _ptc

if TYPE_CHECKING:
    from src.signals.models import SignalEvent

    from .executor import TradeExecutor


@dataclass(frozen=True)
class IntrabarPreTradeResult:
    can_execute: bool
    parent_bar_time: object | None = None
    reject_reason: str | None = None
    detail: str | None = None


class PreTradePipeline:
    """仅保留信号入场前的统一门禁检查。"""

    def __init__(self, executor: "TradeExecutor") -> None:
        self._executor = executor

    def run(self, event: "SignalEvent", timeframe: str) -> str | None:
        return _ptc.run_pre_trade_filters(self._executor, event, timeframe)

    def run_confirmed(self, event: "SignalEvent", timeframe: str) -> str | None:
        return self.run(event, timeframe)

    def run_intrabar(self, event: "SignalEvent", timeframe: str) -> IntrabarPreTradeResult:
        if self._executor.intrabar_guard is None:
            return IntrabarPreTradeResult(
                can_execute=False,
                reject_reason=REASON_INTRABAR_GUARD_MISSING,
            )

        parent_bar_time = event.parent_bar_time or event.metadata.get(
            MK.INTRABAR_PARENT_BAR_TIME
        )
        if parent_bar_time is None:
            return IntrabarPreTradeResult(
                can_execute=False,
                reject_reason=REASON_INTRABAR_PARENT_BAR_MISSING,
            )

        allowed, reason = self._executor.intrabar_guard.can_trade(
            event.symbol,
            event.timeframe,
            event.strategy,
            event.direction,
            parent_bar_time,
        )
        if not allowed:
            return IntrabarPreTradeResult(
                can_execute=False,
                parent_bar_time=parent_bar_time,
                reject_reason=REASON_INTRABAR_GUARD_BLOCKED,
                detail=reason,
            )

        gate_ok, gate_reason = self._executor.execution_gate.check_intrabar(event)
        if not gate_ok:
            return IntrabarPreTradeResult(
                can_execute=False,
                parent_bar_time=parent_bar_time,
                reject_reason=REASON_INTRABAR_GATE_BLOCKED,
                detail=gate_reason,
            )

        reject_reason = self.run(event, timeframe)
        if reject_reason is not None:
            return IntrabarPreTradeResult(
                can_execute=False,
                parent_bar_time=parent_bar_time,
                reject_reason=reject_reason,
            )
        return IntrabarPreTradeResult(
            can_execute=True,
            parent_bar_time=parent_bar_time,
        )
