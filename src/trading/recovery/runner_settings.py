"""Recovery runtime runner settings dataclass (N1: 从 runner.py 拆出)。

独立 dataclass + 4 个 to_*_policy() 转换方法。本文件仅持有配置数据结构，
没有运行时状态——所以可作为 import-stable 端口被 runner.py / 装配层 / 测试桩共用。
"""

from __future__ import annotations

from dataclasses import dataclass

from .calibration_guard import RecoveryCostCalibrationGuardSettings
from .execution import RecoveryExecutionCanaryPolicy
from .models import RecoveryPolicy
from .risk_budget import RecoveryRiskBudgetSettings


@dataclass(frozen=True)
class RecoveryRuntimeRunnerSettings:
    enabled: bool = False
    dry_run: bool = True
    demo_only: bool = True
    symbol: str = "XAUUSD"
    direction: str = "buy"
    strategy: str = "tick_recovery_probe"
    timeframe: str = "TICK"
    base_volume: float = 0.01
    multiplier: float = 2.0
    max_steps: int = 1
    max_total_volume: float = 0.03
    step_distance_points: float = 80.0
    recovery_target_points: float = 5.0
    point: float = 0.01
    max_next_volume: float | None = 0.02
    min_step_interval_ms: int = 0
    max_step_adverse_move_points: float = 0.0
    volume_step: float = 0.01
    contract_size: float = 100.0
    direction_mode: str = "fixed"
    min_directional_move_points: float = 0.0
    max_directional_move_points: float = 0.0
    min_pressure_delta: float = 0.0
    max_entry_spread_points: float | None = None
    slippage_budget_points: float = 0.0
    commission_points: float = 0.0
    min_net_profit_points: float = 0.0
    max_cycle_loss_points: float = 0.0
    max_cycle_duration_seconds: float = 0.0
    max_steps_exit_mode: str = "hold"
    max_steps_hold_seconds: float = 0.0
    entry_confirmation_snapshots: int = 1
    entry_confirmation_max_gap_seconds: float = 3.0
    order_kind: str = "market"
    deviation: int = 20
    magic: int = 0
    comment_prefix: str = "recovery-runner"
    protective_stop_points: float | None = 80.0
    max_cycles_per_session: int = 1
    max_cycles_per_day: int = 0
    min_cycle_interval_seconds: float = 0.0
    cooldown_after_cycle_close_seconds: float = 0.0
    max_cycles_per_hour: int = 0
    snapshot_stale_seconds: float = 5.0
    blocked_dispatch_retry_seconds: float = 30.0
    real_trade_calibration_guard_enabled: bool = True
    real_trade_calibration_min_samples: int = 50
    real_trade_calibration_max_target_shortfall_p90_points: float = 0.0
    real_trade_calibration_min_net_margin_p50_points: float = 0.0
    risk_profile: str = "recovery_budgeted"
    max_daily_recovery_loss_amount: float = 0.0
    max_rolling_recovery_loss_amount: float = 0.0
    rolling_loss_window_minutes: int = 60
    max_consecutive_loss_cycles: int = 0
    loss_lockout_minutes: int = 0

    def __post_init__(self) -> None:
        if not str(self.symbol).strip():
            raise ValueError("symbol is required")
        direction = str(self.direction).strip().lower()
        if direction not in {"buy", "sell"}:
            raise ValueError("direction must be buy or sell")
        object.__setattr__(self, "direction", direction)
        direction_mode = str(self.direction_mode or "fixed").strip().lower()
        if direction_mode not in {"fixed", "auto"}:
            raise ValueError("direction_mode must be fixed or auto")
        object.__setattr__(self, "direction_mode", direction_mode)
        if self.max_cycles_per_session < 0:
            raise ValueError("max_cycles_per_session must be >= 0")
        if self.max_cycles_per_day < 0:
            raise ValueError("max_cycles_per_day must be >= 0")
        if self.min_cycle_interval_seconds < 0:
            raise ValueError("min_cycle_interval_seconds must be >= 0")
        if self.cooldown_after_cycle_close_seconds < 0:
            raise ValueError("cooldown_after_cycle_close_seconds must be >= 0")
        if self.max_cycles_per_hour < 0:
            raise ValueError("max_cycles_per_hour must be >= 0")
        if self.snapshot_stale_seconds <= 0:
            raise ValueError("snapshot_stale_seconds must be > 0")
        if self.blocked_dispatch_retry_seconds < 0:
            raise ValueError("blocked_dispatch_retry_seconds must be >= 0")
        if self.real_trade_calibration_min_samples < 0:
            raise ValueError("real_trade_calibration_min_samples must be >= 0")
        if self.real_trade_calibration_max_target_shortfall_p90_points < 0:
            raise ValueError(
                "real_trade_calibration_max_target_shortfall_p90_points must be >= 0"
            )
        if not str(self.risk_profile).strip():
            raise ValueError("risk_profile is required")
        object.__setattr__(self, "risk_profile", str(self.risk_profile).strip())
        if self.max_daily_recovery_loss_amount < 0:
            raise ValueError("max_daily_recovery_loss_amount must be >= 0")
        if self.max_rolling_recovery_loss_amount < 0:
            raise ValueError("max_rolling_recovery_loss_amount must be >= 0")
        if self.rolling_loss_window_minutes <= 0:
            raise ValueError("rolling_loss_window_minutes must be > 0")
        if self.max_consecutive_loss_cycles < 0:
            raise ValueError("max_consecutive_loss_cycles must be >= 0")
        if self.loss_lockout_minutes < 0:
            raise ValueError("loss_lockout_minutes must be >= 0")
        if self.max_step_adverse_move_points < 0:
            raise ValueError("max_step_adverse_move_points must be >= 0")
        if self.min_directional_move_points < 0:
            raise ValueError("min_directional_move_points must be >= 0")
        if self.max_directional_move_points < 0:
            raise ValueError("max_directional_move_points must be >= 0")
        if self.min_pressure_delta < 0:
            raise ValueError("min_pressure_delta must be >= 0")
        if (
            self.max_entry_spread_points is not None
            and self.max_entry_spread_points <= 0
        ):
            raise ValueError("max_entry_spread_points must be None or > 0")
        if self.slippage_budget_points < 0:
            raise ValueError("slippage_budget_points must be >= 0")
        if self.commission_points < 0:
            raise ValueError("commission_points must be >= 0")
        if self.min_net_profit_points < 0:
            raise ValueError("min_net_profit_points must be >= 0")
        if self.max_cycle_loss_points < 0:
            raise ValueError("max_cycle_loss_points must be >= 0")
        if self.max_cycle_duration_seconds < 0:
            raise ValueError("max_cycle_duration_seconds must be >= 0")
        if self.max_steps_hold_seconds < 0:
            raise ValueError("max_steps_hold_seconds must be >= 0")
        if self.entry_confirmation_snapshots < 0:
            raise ValueError("entry_confirmation_snapshots must be >= 0")
        if self.entry_confirmation_max_gap_seconds < 0:
            raise ValueError("entry_confirmation_max_gap_seconds must be >= 0")
        max_steps_exit_mode = str(self.max_steps_exit_mode or "hold").strip().lower()
        if max_steps_exit_mode not in {"hold", "close_cycle"}:
            raise ValueError("max_steps_exit_mode must be hold or close_cycle")
        object.__setattr__(self, "max_steps_exit_mode", max_steps_exit_mode)
        self.to_recovery_policy()

    def to_recovery_policy(self) -> RecoveryPolicy:
        return RecoveryPolicy(
            enabled=bool(self.enabled),
            base_volume=float(self.base_volume),
            multiplier=float(self.multiplier),
            max_steps=int(self.max_steps),
            max_total_volume=float(self.max_total_volume),
            max_next_volume=(
                None if self.max_next_volume is None else float(self.max_next_volume)
            ),
            step_distance_points=float(self.step_distance_points),
            max_step_adverse_move_points=float(self.max_step_adverse_move_points),
            recovery_target_points=float(self.recovery_target_points),
            point=float(self.point),
            min_step_interval_ms=int(self.min_step_interval_ms),
            volume_step=float(self.volume_step),
            contract_size=float(self.contract_size),
            direction_mode=str(self.direction_mode),
            min_directional_move_points=float(self.min_directional_move_points),
            max_directional_move_points=float(self.max_directional_move_points),
            min_pressure_delta=float(self.min_pressure_delta),
            max_entry_spread_points=(
                None
                if self.max_entry_spread_points is None
                else float(self.max_entry_spread_points)
            ),
            slippage_budget_points=float(self.slippage_budget_points),
            commission_points=float(self.commission_points),
            min_net_profit_points=float(self.min_net_profit_points),
            max_cycle_loss_points=float(self.max_cycle_loss_points),
            max_cycle_duration_seconds=float(self.max_cycle_duration_seconds),
            max_steps_exit_mode=str(self.max_steps_exit_mode),
            max_steps_hold_seconds=float(self.max_steps_hold_seconds),
        )

    def to_canary_policy(self) -> RecoveryExecutionCanaryPolicy:
        return RecoveryExecutionCanaryPolicy(
            enabled=bool(self.enabled),
            dry_run=bool(self.dry_run),
            order_kind=str(self.order_kind or "market"),
            deviation=int(self.deviation),
            magic=int(self.magic),
            comment_prefix=str(self.comment_prefix or "recovery-runner"),
            protective_stop_points=self.protective_stop_points,
        )

    def to_calibration_guard_settings(self) -> RecoveryCostCalibrationGuardSettings:
        return RecoveryCostCalibrationGuardSettings(
            enabled=bool(self.real_trade_calibration_guard_enabled),
            min_samples=int(self.real_trade_calibration_min_samples),
            max_target_shortfall_p90_points=float(
                self.real_trade_calibration_max_target_shortfall_p90_points
            ),
            min_net_margin_p50_points=float(
                self.real_trade_calibration_min_net_margin_p50_points
            ),
        )

    def to_risk_budget_settings(self) -> RecoveryRiskBudgetSettings:
        return RecoveryRiskBudgetSettings(
            risk_profile=str(self.risk_profile),
            max_daily_recovery_loss_amount=float(self.max_daily_recovery_loss_amount),
            max_rolling_recovery_loss_amount=float(
                self.max_rolling_recovery_loss_amount
            ),
            rolling_loss_window_minutes=int(self.rolling_loss_window_minutes),
            max_consecutive_loss_cycles=int(self.max_consecutive_loss_cycles),
            loss_lockout_minutes=int(self.loss_lockout_minutes),
        )
