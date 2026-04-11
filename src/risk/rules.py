from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Protocol

from src.config import EconomicConfig, RiskConfig
from src.signals.contracts import normalize_session_name
from src.signals.execution.filters import SessionFilter

from .models import RiskCheckResult, TradeIntent


class EconomicCalendarRuleProvider(Protocol):
    def is_stale(self) -> bool: ...

    def stats(self) -> Dict[str, Any]: ...

    def get_trade_guard(self, **kwargs: Any) -> Dict[str, Any]: ...


class AccountStateProvider(Protocol):
    def account_info(self) -> AccountSnapshot: ...

    def positions(self, symbol: Optional[str] = None) -> List[PositionSnapshot]:
        ...

    def orders(self, symbol: Optional[str] = None) -> List[PositionSnapshot]:
        ...


class PositionSnapshot(Protocol):
    type: int
    volume: float


class AccountSnapshot(Protocol):
    balance: float
    equity: float
    margin_free: float | None
    day_start_balance: float | None
    daily_realized_pnl: float | None
    daily_pnl: float | None


@dataclass
class RuleContext:
    intent: TradeIntent
    economic_settings: EconomicConfig
    risk_settings: RiskConfig
    economic_provider: Optional[EconomicCalendarRuleProvider] = None
    account_provider: Optional[AccountStateProvider] = None
    mode: str = "warn_only"
    calendar_health_mode: str = "warn_only"


class RiskRule:
    name: str

    def evaluate(self, context: RuleContext) -> List[RiskCheckResult]:
        raise NotImplementedError


class AccountSnapshotRule(RiskRule):
    name = "account_snapshot"

    def evaluate(self, context: RuleContext) -> List[RiskCheckResult]:
        if not context.risk_settings.enabled or context.account_provider is None:
            return []

        checks: List[RiskCheckResult] = []
        try:
            symbol_positions = list(
                context.account_provider.positions(context.intent.symbol)
            )
            all_positions = list(context.account_provider.positions())
            symbol_orders = list(context.account_provider.orders(context.intent.symbol))
        except Exception as exc:
            return [
                RiskCheckResult(
                    name=self.name,
                    verdict="warn",
                    reason="Account state unavailable for structural risk checks",
                    details={"error": str(exc)},
                )
            ]

        settings = context.risk_settings
        if (
            settings.max_volume_per_order is not None
            and context.intent.volume > settings.max_volume_per_order
        ):
            checks.append(
                RiskCheckResult(
                    name="max_volume_per_order",
                    verdict="block",
                    reason="Requested volume exceeds per-order limit",
                    details={
                        "requested_volume": context.intent.volume,
                        "limit": settings.max_volume_per_order,
                    },
                )
            )

        if (
            settings.max_positions_per_symbol is not None
            and len(symbol_positions) >= settings.max_positions_per_symbol
        ):
            checks.append(
                RiskCheckResult(
                    name="max_positions_per_symbol",
                    verdict="block",
                    reason="Open positions for symbol already at configured limit",
                    details={
                        "symbol": context.intent.symbol,
                        "open_positions": len(symbol_positions),
                        "limit": settings.max_positions_per_symbol,
                    },
                )
            )

        if (
            settings.max_open_positions_total is not None
            and len(all_positions) >= settings.max_open_positions_total
        ):
            checks.append(
                RiskCheckResult(
                    name="max_open_positions_total",
                    verdict="block",
                    reason="Total open positions already at configured limit",
                    details={
                        "open_positions": len(all_positions),
                        "limit": settings.max_open_positions_total,
                    },
                )
            )

        if (
            settings.max_pending_orders_per_symbol is not None
            and len(symbol_orders) >= settings.max_pending_orders_per_symbol
        ):
            checks.append(
                RiskCheckResult(
                    name="max_pending_orders_per_symbol",
                    verdict="block",
                    reason="Pending orders for symbol already at configured limit",
                    details={
                        "symbol": context.intent.symbol,
                        "pending_orders": len(symbol_orders),
                        "limit": settings.max_pending_orders_per_symbol,
                    },
                )
            )

        if settings.max_volume_per_symbol is not None:
            current_symbol_volume = sum(
                float(position.volume or 0.0) for position in symbol_positions
            )
            projected_volume = current_symbol_volume + context.intent.volume
            if projected_volume > settings.max_volume_per_symbol:
                checks.append(
                    RiskCheckResult(
                        name="max_volume_per_symbol",
                        verdict="block",
                        reason="Projected symbol exposure exceeds configured limit",
                        details={
                            "symbol": context.intent.symbol,
                            "current_volume": current_symbol_volume,
                            "requested_volume": context.intent.volume,
                            "projected_volume": projected_volume,
                            "limit": settings.max_volume_per_symbol,
                        },
                    )
                )

        if settings.max_net_lots_per_symbol is not None:
            intent_side = str(context.intent.side or "").strip().lower()
            if intent_side in {"buy", "sell"}:
                same_side_volume = 0.0
                for position in symbol_positions:
                    # 单品种 MT5 持仓方向：type 0=buy, 1=sell
                    pos_type = int(position.type)
                    if (intent_side == "buy" and pos_type == 0) or (
                        intent_side == "sell" and pos_type == 1
                    ):
                        same_side_volume += float(
                            position.volume or 0.0
                        )
                projected_net_lots = same_side_volume + float(
                    context.intent.volume or 0.0
                )
                if projected_net_lots > settings.max_net_lots_per_symbol:
                    checks.append(
                        RiskCheckResult(
                            name="max_net_lots_per_symbol",
                            verdict="block",
                            reason="Projected same-direction net lots exceed configured limit",
                            details={
                                "symbol": context.intent.symbol,
                                "side": intent_side,
                                "current_same_side_lots": round(same_side_volume, 4),
                                "requested_volume": context.intent.volume,
                                "projected_net_lots": round(projected_net_lots, 4),
                                "limit": settings.max_net_lots_per_symbol,
                            },
                        )
                    )

        return checks


class ProtectionRule(RiskRule):
    name = "protection"

    def evaluate(self, context: RuleContext) -> List[RiskCheckResult]:
        if not context.risk_settings.enabled:
            return []
        if str(context.intent.order_kind or "market").lower() != "market":
            return []

        checks: List[RiskCheckResult] = []
        mode = (
            str(context.risk_settings.market_order_protection or "off").strip().lower()
        )
        if mode == "off":
            return checks

        if mode == "sl" and context.intent.sl is None:
            checks.append(
                RiskCheckResult(
                    name="market_order_protection",
                    verdict="block",
                    reason="Stop loss is required for market orders",
                    details={"symbol": context.intent.symbol, "mode": mode},
                )
            )

        if (
            mode == "sl_or_tp"
            and context.intent.sl is None
            and context.intent.tp is None
        ):
            checks.append(
                RiskCheckResult(
                    name="market_order_protection",
                    verdict="block",
                    reason="At least one protective exit is required for market orders",
                    details={"symbol": context.intent.symbol, "mode": mode},
                )
            )
        return checks


class DailyLossLimitRule(RiskRule):
    name = "daily_loss_limit"

    def evaluate(self, context: RuleContext) -> List[RiskCheckResult]:
        if not context.risk_settings.enabled or context.account_provider is None:
            return []
        limit_pct = context.risk_settings.daily_loss_limit_pct
        if limit_pct is None or float(limit_pct) <= 0:
            return []

        try:
            account_info = context.account_provider.account_info()
        except Exception as exc:
            return [
                RiskCheckResult(
                    name=self.name,
                    verdict="warn",
                    reason="Daily loss limit unavailable because account info could not be loaded",
                    details={"error": str(exc)},
                )
            ]

        details = self._resolve_loss_details(context, account_info)
        loss_pct = details.get("loss_pct")
        if loss_pct is None or float(loss_pct) < float(limit_pct):
            return []

        return [
            RiskCheckResult(
                name=self.name,
                verdict="block",
                reason="daily_loss_limit_reached",
                details={
                    **details,
                    "limit_pct": float(limit_pct),
                },
            )
        ]

    @staticmethod
    def _numeric(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _resolve_loss_details(
        self,
        context: RuleContext,
        account_info: Any,
    ) -> Dict[str, Any]:
        metadata = dict(context.intent.metadata or {})
        explicit_loss_pct = self._numeric(metadata.get("daily_loss_pct"))
        if explicit_loss_pct is not None:
            return {
                "loss_pct": max(explicit_loss_pct, 0.0),
                "source": "intent_metadata.daily_loss_pct",
            }

        balance = self._numeric(account_info.balance)
        equity = self._numeric(account_info.equity)
        start_balance = self._numeric(
            metadata.get("day_start_balance")
            if "day_start_balance" in metadata
            else account_info.day_start_balance
        )
        daily_pnl = self._numeric(
            metadata.get("daily_realized_pnl")
            if "daily_realized_pnl" in metadata
            else account_info.daily_realized_pnl
        )
        if daily_pnl is None:
            daily_pnl = self._numeric(
                metadata.get("daily_pnl")
                if "daily_pnl" in metadata
                else account_info.daily_pnl
            )

        # 当两个数据源都可用时，取更保守（更高亏损）的估计。
        # daily_pnl 可能滞后同步，equity 始终反映实时浮动盈亏。
        pnl_loss: float | None = None
        equity_loss: float | None = None
        if start_balance and start_balance > 0:
            if daily_pnl is not None:
                pnl_loss = max(0.0, (-daily_pnl / start_balance) * 100.0)
            if equity is not None:
                equity_loss = max(
                    0.0, ((start_balance - equity) / start_balance) * 100.0
                )

        if pnl_loss is not None and equity_loss is not None:
            # 交叉验证：取保守值，附带两个数据源供诊断
            loss_pct = max(pnl_loss, equity_loss)
            return {
                "loss_pct": round(loss_pct, 4),
                "source": "cross_validated",
                "day_start_balance": start_balance,
                "daily_pnl": daily_pnl,
                "equity": equity,
                "pnl_loss_pct": round(pnl_loss, 4),
                "equity_loss_pct": round(equity_loss, 4),
            }
        if pnl_loss is not None:
            return {
                "loss_pct": round(pnl_loss, 4),
                "source": "daily_pnl_vs_day_start_balance",
                "day_start_balance": start_balance,
                "daily_pnl": daily_pnl,
            }
        if equity_loss is not None:
            return {
                "loss_pct": round(equity_loss, 4),
                "source": "equity_vs_day_start_balance",
                "day_start_balance": start_balance,
                "equity": equity,
            }

        if balance is not None and equity is not None and balance > 0:
            # Conservative fallback: treat current floating drawdown vs balance
            # as the daily-loss proxy when no day-start balance is available.
            loss_pct = max(0.0, ((balance - equity) / balance) * 100.0)
            return {
                "loss_pct": round(loss_pct, 4),
                "source": "equity_balance_drawdown_proxy",
                "balance": balance,
                "equity": equity,
            }

        return {
            "loss_pct": None,
            "source": "unavailable",
        }


class MarginAvailabilityRule(RiskRule):
    """Blocks trades when estimated margin exceeds available free margin.

    Reads ``estimated_margin`` from ``intent.metadata`` and ``margin_free``
    from the account provider.  A configurable *safety factor* (default 1.2)
    ensures a buffer above the minimum margin requirement.
    """

    name = "margin_availability"

    def evaluate(self, context: RuleContext) -> List[RiskCheckResult]:
        if not context.risk_settings.enabled or context.account_provider is None:
            return []
        safety = float(context.risk_settings.margin_safety_factor or 0)
        if safety <= 0:
            return []

        estimated_margin = self._numeric(
            (context.intent.metadata or {}).get("estimated_margin")
        )
        if estimated_margin is None or estimated_margin <= 0:
            return []

        try:
            account_info = context.account_provider.account_info()
        except Exception:
            return [
                RiskCheckResult(
                    name=self.name,
                    verdict="warn",
                    reason="Margin check unavailable: account info could not be loaded",
                )
            ]

        free_margin = self._numeric(account_info.margin_free)

        if free_margin is None:
            return [
                RiskCheckResult(
                    name=self.name,
                    verdict="warn",
                    reason="Free margin data unavailable from account info",
                    details={"estimated_margin": estimated_margin},
                )
            ]

        required = estimated_margin * safety
        if free_margin >= required:
            return []

        return [
            RiskCheckResult(
                name=self.name,
                verdict="block",
                reason="Insufficient free margin for trade",
                details={
                    "estimated_margin": round(estimated_margin, 2),
                    "required_margin": round(required, 2),
                    "free_margin": round(free_margin, 2),
                    "safety_factor": safety,
                    "shortfall": round(required - free_margin, 2),
                },
            )
        ]

    @staticmethod
    def _numeric(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


class TradeFrequencyRule(RiskRule):
    """Limits the number of trades per day and per hour.

    This is a *stateful* rule: call :meth:`record_trade` after every
    successful trade execution so the rule can track timestamps.
    """

    name = "trade_frequency"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._trade_timestamps: List[datetime] = []

    def record_trade(self, at: Optional[datetime] = None) -> None:
        """Record a successful trade execution timestamp (thread-safe)."""
        now = at or datetime.now(timezone.utc)
        with self._lock:
            self._trade_timestamps.append(now)
            # Prune entries older than 48h to bound memory
            cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
            self._trade_timestamps = [t for t in self._trade_timestamps if t > cutoff]

    def evaluate(self, context: RuleContext) -> List[RiskCheckResult]:
        if not context.risk_settings.enabled:
            return []

        checks: List[RiskCheckResult] = []
        now = datetime.now(timezone.utc)
        with self._lock:
            timestamps = list(self._trade_timestamps)

        max_per_day = context.risk_settings.max_trades_per_day
        if max_per_day is not None and max_per_day > 0:
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            day_count = sum(1 for t in timestamps if t >= day_start)
            if day_count >= max_per_day:
                checks.append(
                    RiskCheckResult(
                        name="max_trades_per_day",
                        verdict="block",
                        reason="Daily trade limit reached",
                        details={
                            "trades_today": day_count,
                            "limit": max_per_day,
                        },
                    )
                )

        max_per_hour = context.risk_settings.max_trades_per_hour
        if max_per_hour is not None and max_per_hour > 0:
            hour_ago = now - timedelta(hours=1)
            hour_count = sum(1 for t in timestamps if t >= hour_ago)
            if hour_count >= max_per_hour:
                checks.append(
                    RiskCheckResult(
                        name="max_trades_per_hour",
                        verdict="block",
                        reason="Hourly trade limit reached",
                        details={
                            "trades_last_hour": hour_count,
                            "limit": max_per_hour,
                        },
                    )
                )

        return checks


class SessionWindowRule(RiskRule):
    name = "allowed_sessions"

    def evaluate(self, context: RuleContext) -> List[RiskCheckResult]:
        if not context.risk_settings.enabled:
            return []
        raw_allowed = str(context.risk_settings.allowed_sessions or "").strip()
        if not raw_allowed:
            return []

        allowed_sessions = {
            normalize_session_name(item)
            for item in raw_allowed.split(",")
            if str(item).strip()
        }
        current_time = context.intent.at_time or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        else:
            current_time = current_time.astimezone(timezone.utc)
        current_sessions = SessionFilter().current_sessions(current_time)
        if any(session_name in allowed_sessions for session_name in current_sessions):
            return []

        return [
            RiskCheckResult(
                name=self.name,
                verdict="block",
                reason=f"outside_allowed_sessions:{','.join(current_sessions)}",
                details={
                    "current_sessions": current_sessions,
                    "allowed_sessions": sorted(allowed_sessions),
                },
            )
        ]


def _market_structure(context: RuleContext) -> Dict[str, Any]:
    payload = context.intent.metadata.get("market_structure")
    return dict(payload) if isinstance(payload, dict) else {}


def _normalized_side(side: str) -> str:
    side_value = str(side or "").strip().lower()
    if side_value in {"buy", "long"}:
        return "buy"
    if side_value in {"sell", "short"}:
        return "sell"
    return side_value


def _has_directional_structure(structure: Dict[str, Any], direction: str) -> bool:
    prefix = f"{direction}_"
    for key in (
        "structure_bias",
        "reclaim_state",
        "first_pullback_state",
        "sweep_confirmation_state",
    ):
        if str(structure.get(key) or "").strip().lower().startswith(prefix):
            return True

    breakout_state = str(structure.get("breakout_state") or "").strip().lower()
    if direction == "bullish":
        return breakout_state in {
            "above_previous_day_high",
            "above_asia_range_high",
            "above_london_open_high",
            "above_new_york_open_high",
        }
    if direction == "bearish":
        return breakout_state in {
            "below_previous_day_low",
            "below_asia_range_low",
            "below_london_open_low",
            "below_new_york_open_low",
        }
    return False


class MarketStructureRule(RiskRule):
    name = "market_structure"

    def evaluate(self, context: RuleContext) -> List[RiskCheckResult]:
        if not context.risk_settings.enabled:
            return []

        structure = _market_structure(context)
        if not structure:
            return []

        checks: List[RiskCheckResult] = []
        side = _normalized_side(context.intent.side)
        sweep_confirmation_state = (
            str(structure.get("sweep_confirmation_state") or "none").strip().lower()
        )
        confirmation_reference = structure.get("confirmation_reference")

        if side == "buy" and sweep_confirmation_state.startswith("bearish_"):
            checks.append(
                RiskCheckResult(
                    name=self.name,
                    verdict="block",
                    reason="buy_conflicts_with_bearish_sweep_confirmation",
                    details={
                        "side": side,
                        "sweep_confirmation_state": sweep_confirmation_state,
                        "confirmation_reference": confirmation_reference,
                        "structure_bias": structure.get("structure_bias"),
                    },
                )
            )
        elif side == "sell" and sweep_confirmation_state.startswith("bullish_"):
            checks.append(
                RiskCheckResult(
                    name=self.name,
                    verdict="block",
                    reason="sell_conflicts_with_bullish_sweep_confirmation",
                    details={
                        "side": side,
                        "sweep_confirmation_state": sweep_confirmation_state,
                        "confirmation_reference": confirmation_reference,
                        "structure_bias": structure.get("structure_bias"),
                    },
                )
            )

        current_session = normalize_session_name(structure.get("current_session") or "")
        breakout_state = str(structure.get("breakout_state") or "none").strip().lower()
        if current_session == "new_york":
            if (
                side == "buy"
                and breakout_state == "below_new_york_open_low"
                and not _has_directional_structure(structure, "bullish")
            ):
                checks.append(
                    RiskCheckResult(
                        name=f"{self.name}_new_york_open",
                        verdict="warn",
                        reason="buy_against_new_york_open_downside_expansion",
                        details={
                            "side": side,
                            "current_session": current_session,
                            "breakout_state": breakout_state,
                            "new_york_open_low": structure.get("new_york_open_low"),
                        },
                    )
                )
            elif (
                side == "sell"
                and breakout_state == "above_new_york_open_high"
                and not _has_directional_structure(structure, "bearish")
            ):
                checks.append(
                    RiskCheckResult(
                        name=f"{self.name}_new_york_open",
                        verdict="warn",
                        reason="sell_against_new_york_open_upside_expansion",
                        details={
                            "side": side,
                            "current_session": current_session,
                            "breakout_state": breakout_state,
                            "new_york_open_high": structure.get("new_york_open_high"),
                        },
                    )
                )

        return checks


class EconomicEventRule(RiskRule):
    name = "economic_event"

    def evaluate(self, context: RuleContext) -> List[RiskCheckResult]:
        provider = context.economic_provider
        settings = context.economic_settings
        if not settings.enabled or not settings.trade_guard_enabled or provider is None:
            return []
        assessment = provider.get_trade_guard(
            symbol=context.intent.symbol,
            at_time=context.intent.at_time,
            lookahead_minutes=settings.trade_guard_lookahead_minutes,
            lookback_minutes=settings.trade_guard_lookback_minutes,
            importance_min=settings.trade_guard_importance_min,
        )
        if not bool(assessment.get("blocked")):
            return []
        active_windows = assessment.get("active_windows") or []
        reason = (
            f"High-impact economic event window active ({len(active_windows)} window(s))"
            if active_windows
            else "High-impact economic event risk detected"
        )
        action = "block" if context.mode == "block" else "warn"
        return [
            RiskCheckResult(
                name=self.name,
                verdict=action,
                reason=reason,
                details={"assessment": assessment},
            )
        ]


class CalendarHealthRule(RiskRule):
    name = "calendar_health"

    def evaluate(self, context: RuleContext) -> List[RiskCheckResult]:
        provider = context.economic_provider
        settings = context.economic_settings
        if not settings.enabled or not settings.trade_guard_enabled or provider is None:
            return []

        stats = provider.stats()
        provider_status = stats.get("provider_status") or {}
        failure_threshold = max(0, int(settings.trade_guard_provider_failure_threshold))
        stale = bool(provider.is_stale())
        failing_providers = []
        if failure_threshold > 0:
            for provider_name, state in provider_status.items():
                if not bool(state.get("enabled", True)):
                    continue
                consecutive_failures = int(state.get("consecutive_failures") or 0)
                if consecutive_failures >= failure_threshold:
                    failing_providers.append(
                        {
                            "provider": provider_name,
                            "consecutive_failures": consecutive_failures,
                            "last_error": state.get("last_error"),
                        }
                    )
        if not stale and not failing_providers:
            return []

        reasons = []
        if stale:
            reasons.append("Economic calendar data is stale")
        if failing_providers:
            failing_names = ", ".join(item["provider"] for item in failing_providers)
            reasons.append(f"Economic calendar providers degraded: {failing_names}")

        action = "warn"
        if context.calendar_health_mode == "fail_closed":
            action = "block"

        return [
            RiskCheckResult(
                name=self.name,
                verdict=action,
                reason="; ".join(reasons),
                details={
                    "stale": stale,
                    "failing_providers": failing_providers,
                    "provider_failure_threshold": failure_threshold,
                    "provider_status": provider_status,
                    "last_refresh_at": stats.get("last_refresh_at"),
                    "refresh_in_progress": stats.get("refresh_in_progress"),
                },
            )
        ]
