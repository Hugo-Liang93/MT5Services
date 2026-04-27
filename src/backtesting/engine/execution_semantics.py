"""Backtest execution semantics.

本模块只负责“信号是否能转成成交动作”的语义差异，不参与：
- 指标计算
- 策略评估
- regime / voting / filters

这样可以在保持共享信号内核的同时，显式区分：
- research：研究型回测，允许理论小数仓位
- execution_feasibility：可执行性模拟，要求满足最小手数等执行约束
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from src.backtesting.models import BacktestConfig, SimulationMode
from src.trading.execution.sizing import (
    TradeParameters,
    resolve_timeframe_risk_multiplier,
)

_DEFAULT_VOLUME_STEP = 0.01


@dataclass(frozen=True)
class ExecutionResolution:
    """执行语义决策结果。"""

    accepted: bool
    trade_params: TradeParameters | None
    reason: str = ""
    raw_position_size: float = 0.0
    resolved_position_size: float = 0.0


def resolve_trade_parameters(
    config: BacktestConfig,
    trade_params: TradeParameters,
    *,
    account_balance: float,
) -> ExecutionResolution:
    """按 simulation_mode 调整/校验成交手数。"""

    raw_position_size = _compute_raw_position_size(
        config,
        trade_params.sl_distance,
        account_balance=account_balance,
    )
    if raw_position_size <= 0:
        return ExecutionResolution(
            accepted=False,
            trade_params=None,
            reason="non_positive_theoretical_volume",
            raw_position_size=raw_position_size,
        )

    if config.simulation_mode is SimulationMode.RESEARCH:
        resolved_size = _resolve_research_position_size(config, raw_position_size)
        if resolved_size <= 0:
            return ExecutionResolution(
                accepted=False,
                trade_params=None,
                reason="non_positive_research_volume",
                raw_position_size=raw_position_size,
            )
        return ExecutionResolution(
            accepted=True,
            trade_params=replace(trade_params, position_size=resolved_size),
            raw_position_size=raw_position_size,
            resolved_position_size=resolved_size,
        )

    resolved_size, reject_reason = _resolve_execution_feasible_position_size(
        config,
        raw_position_size,
        sl_distance=trade_params.sl_distance,
        account_balance=account_balance,
    )
    if resolved_size is None:
        return ExecutionResolution(
            accepted=False,
            trade_params=None,
            reason=reject_reason or "below_min_volume_for_execution_feasibility",
            raw_position_size=raw_position_size,
        )
    return ExecutionResolution(
        accepted=True,
        trade_params=replace(trade_params, position_size=resolved_size),
        raw_position_size=raw_position_size,
        resolved_position_size=resolved_size,
    )


def _compute_raw_position_size(
    config: BacktestConfig,
    sl_distance: float,
    *,
    account_balance: float,
) -> float:
    if sl_distance <= 0:
        return 0.0
    account_balance = float(account_balance)
    if account_balance <= 0:
        return 0.0

    risk_pct = float(config.position.risk_percent)
    effective_risk_pct = risk_pct * resolve_timeframe_risk_multiplier(config.timeframe)
    if effective_risk_pct <= 0:
        return 0.0

    contract_size = float(config.position.contract_size)
    if contract_size <= 0:
        return 0.0

    risk_amount = account_balance * (effective_risk_pct / 100.0)
    risk_per_lot = sl_distance * contract_size
    if risk_per_lot <= 0:
        return 0.0

    return risk_amount / risk_per_lot


def _resolve_research_position_size(
    config: BacktestConfig,
    raw_position_size: float,
) -> float:
    if raw_position_size <= 0:
        return 0.0
    return round(min(config.position.max_volume, raw_position_size), 8)


def _resolve_execution_feasible_position_size(
    config: BacktestConfig,
    raw_position_size: float,
    *,
    sl_distance: float,
    account_balance: float,
) -> tuple[float | None, str | None]:
    """返回 (resolved_size, reject_reason)。

    raw ≥ min_volume：按 step floor 后 cap 到 max_volume，正常返回。
    raw < min_volume：
      - allow_min_volume_fallback=False（默认）→ 拒（broker semantic）
      - allow_min_volume_fallback=True → 检查 0.01 lot 实际风险占比：
          - 不超过 max_actual_risk_pct → 强制 min_volume，accept
          - 超过 max_actual_risk_pct → 拒（避免小账户大 SL 吃账户）
    """
    min_volume = float(config.position.min_volume)
    max_volume = float(config.position.max_volume)
    if max_volume < min_volume:
        return None, "max_volume_below_min_volume"

    step = _execution_volume_step(min_volume)
    aligned = _align_volume(raw_position_size, step)
    if aligned >= min_volume:
        return round(min(max_volume, aligned), 8), None

    # raw < min_volume —— 走 fallback 决策
    if not config.position.allow_min_volume_fallback:
        return None, "below_min_volume_for_execution_feasibility"

    contract_size = float(config.position.contract_size)
    if account_balance <= 0:
        return None, "below_min_volume_for_execution_feasibility"

    actual_risk_amount = min_volume * sl_distance * contract_size
    actual_risk_pct = (actual_risk_amount / account_balance) * 100.0
    max_actual_risk = float(config.position.max_actual_risk_pct)
    if actual_risk_pct > max_actual_risk:
        return None, "exceeds_max_actual_risk_pct"

    return round(min_volume, 8), None


def _align_volume(value: float, step: float) -> float:
    if value <= 0:
        return 0.0
    if step <= 0:
        return round(value, 8)
    return round(math.floor(value / step) * step, 8)


def _execution_volume_step(min_volume: float) -> float:
    if min_volume > 0:
        return min(_DEFAULT_VOLUME_STEP, min_volume)
    return _DEFAULT_VOLUME_STEP
