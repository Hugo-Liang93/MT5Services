"""Trade 执行决策组件。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .params import compute_params as _compute_params_helper
from .params import estimate_cost_metrics as _estimate_cost_metrics_helper
from .eventing import execute_market_order as _execute_market_order_helper
from .pending_orders import submit_pending_entry as _submit_pending_entry_helper
from .reasons import (
    REASON_SPREAD_TO_STOP_RATIO_TOO_HIGH,
    REASON_TRADE_PARAMS_UNAVAILABLE,
)
from src.signals.metadata_keys import MetadataKey as MK

if TYPE_CHECKING:
    from src.signals.models import SignalEvent

    from .executor import TradeExecutor


@dataclass(frozen=True)
class ExecutionDecision:
    trade_params: object | None
    cost_metrics: dict[str, float | None] | None
    use_market: bool
    entry_type: str
    reject_reason: str | None = None
    spread_to_stop_ratio: float | None = None


class ExecutionDecisionEngine:
    """将“决策计算”与执行动作解耦。"""

    def __init__(self, executor: "TradeExecutor") -> None:
        self._executor = executor

    def build_confirmed_decision(self, event: "SignalEvent") -> ExecutionDecision:
        trade_params = _compute_params_helper(self._executor, event)
        if trade_params is None:
            return ExecutionDecision(
                trade_params=None,
                cost_metrics=None,
                use_market=True,
                entry_type="market",
                reject_reason=REASON_TRADE_PARAMS_UNAVAILABLE,
            )

        cost_metrics = _estimate_cost_metrics_helper(self._executor, event, trade_params)
        spread_to_stop_ratio = cost_metrics.get("spread_to_stop_ratio")
        if (
            spread_to_stop_ratio is not None
            and spread_to_stop_ratio > self._executor.config.max_spread_to_stop_ratio
        ):
            return ExecutionDecision(
                trade_params=trade_params,
                cost_metrics=cost_metrics,
                use_market=True,
                entry_type="market",
                reject_reason=REASON_SPREAD_TO_STOP_RATIO_TOO_HIGH,
                spread_to_stop_ratio=spread_to_stop_ratio,
            )

        entry_spec = event.metadata.get(MK.ENTRY_SPEC, {})
        entry_type = (
            entry_spec.get("entry_type", "market") if isinstance(entry_spec, dict) else "market"
        )
        use_market = entry_type == "market" or self._executor.pending_manager is None
        return ExecutionDecision(
            trade_params=trade_params,
            cost_metrics=cost_metrics,
            use_market=use_market,
            entry_type=entry_type,
            spread_to_stop_ratio=spread_to_stop_ratio,
        )

    def execute(self, event: "SignalEvent", decision: ExecutionDecision) -> Any | None:
        if decision.trade_params is None:
            return None
        if decision.use_market or self._executor.pending_manager is None:
            return _execute_market_order_helper(
                self._executor, event, decision.trade_params, cost_metrics=decision.cost_metrics
            )
        return _submit_pending_entry_helper(
            self._executor, event, decision.trade_params, cost_metrics=decision.cost_metrics
        )

    def build_intrabar_decision(self, event: "SignalEvent") -> ExecutionDecision:
        trade_params = _compute_params_helper(self._executor, event)
        if trade_params is None:
            return ExecutionDecision(
                trade_params=None,
                cost_metrics=None,
                use_market=True,
                entry_type="market",
                reject_reason=REASON_TRADE_PARAMS_UNAVAILABLE,
            )
        cost_metrics = _estimate_cost_metrics_helper(self._executor, event, trade_params)
        entry_spec = event.metadata.get(MK.ENTRY_SPEC, {})
        entry_type = (
            entry_spec.get("entry_type", "market") if isinstance(entry_spec, dict) else "market"
        )
        return ExecutionDecision(
            trade_params=trade_params,
            cost_metrics=cost_metrics,
            use_market=entry_type == "market" or self._executor.pending_manager is None,
            entry_type=entry_type,
        )
