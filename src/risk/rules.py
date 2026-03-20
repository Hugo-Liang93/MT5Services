from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

from src.config import EconomicConfig, RiskConfig
from src.signals.execution.filters import SessionFilter
from src.signals.contracts import normalize_session_name

from .models import RiskCheckResult, TradeIntent


class EconomicCalendarRuleProvider(Protocol):
    def is_stale(self) -> bool:
        ...

    def stats(self) -> Dict[str, Any]:
        ...

    def get_trade_guard(self, **kwargs: Any) -> Dict[str, Any]:
        ...


class AccountStateProvider(Protocol):
    def account_info(self):
        ...

    def positions(self, symbol: Optional[str] = None):
        ...

    def orders(self, symbol: Optional[str] = None):
        ...


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
            symbol_positions = list(context.account_provider.positions(context.intent.symbol))
            all_positions = list(context.account_provider.positions())
            symbol_orders = list(context.account_provider.orders(context.intent.symbol))
        except Exception as exc:
            return [
                RiskCheckResult(
                    name=self.name,
                    action="warn",
                    reason="Account state unavailable for structural risk checks",
                    details={"error": str(exc)},
                )
            ]

        settings = context.risk_settings
        if settings.max_volume_per_order is not None and context.intent.volume > settings.max_volume_per_order:
            checks.append(
                RiskCheckResult(
                    name="max_volume_per_order",
                    action="block",
                    reason="Requested volume exceeds per-order limit",
                    details={
                        "requested_volume": context.intent.volume,
                        "limit": settings.max_volume_per_order,
                    },
                )
            )

        if settings.max_positions_per_symbol is not None and len(symbol_positions) >= settings.max_positions_per_symbol:
            checks.append(
                RiskCheckResult(
                    name="max_positions_per_symbol",
                    action="block",
                    reason="Open positions for symbol already at configured limit",
                    details={
                        "symbol": context.intent.symbol,
                        "open_positions": len(symbol_positions),
                        "limit": settings.max_positions_per_symbol,
                    },
                )
            )

        if settings.max_open_positions_total is not None and len(all_positions) >= settings.max_open_positions_total:
            checks.append(
                RiskCheckResult(
                    name="max_open_positions_total",
                    action="block",
                    reason="Total open positions already at configured limit",
                    details={
                        "open_positions": len(all_positions),
                        "limit": settings.max_open_positions_total,
                    },
                )
            )

        if settings.max_pending_orders_per_symbol is not None and len(symbol_orders) >= settings.max_pending_orders_per_symbol:
            checks.append(
                RiskCheckResult(
                    name="max_pending_orders_per_symbol",
                    action="block",
                    reason="Pending orders for symbol already at configured limit",
                    details={
                        "symbol": context.intent.symbol,
                        "pending_orders": len(symbol_orders),
                        "limit": settings.max_pending_orders_per_symbol,
                    },
                )
            )

        if settings.max_volume_per_symbol is not None:
            current_symbol_volume = sum(float(position.volume or 0.0) for position in symbol_positions)
            projected_volume = current_symbol_volume + context.intent.volume
            if projected_volume > settings.max_volume_per_symbol:
                checks.append(
                    RiskCheckResult(
                        name="max_volume_per_symbol",
                        action="block",
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

        return checks


class ProtectionRule(RiskRule):
    name = "protection"

    def evaluate(self, context: RuleContext) -> List[RiskCheckResult]:
        if not context.risk_settings.enabled:
            return []
        if str(context.intent.order_kind or "market").lower() != "market":
            return []

        checks: List[RiskCheckResult] = []
        if context.risk_settings.require_sl_for_market_orders and context.intent.sl is None:
            checks.append(
                RiskCheckResult(
                    name="require_sl_for_market_orders",
                    action="block",
                    reason="Stop loss is required for market orders",
                    details={"symbol": context.intent.symbol},
                )
            )

        if (
            context.risk_settings.require_tp_or_sl_for_market_orders
            and context.intent.sl is None
            and context.intent.tp is None
        ):
            checks.append(
                RiskCheckResult(
                    name="require_tp_or_sl_for_market_orders",
                    action="block",
                    reason="At least one protective exit is required for market orders",
                    details={"symbol": context.intent.symbol},
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
                    action="warn",
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
                action="block",
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

        if isinstance(account_info, dict):
            balance = self._numeric(account_info.get("balance"))
            equity = self._numeric(account_info.get("equity"))
            start_balance = self._numeric(
                account_info.get("day_start_balance") or metadata.get("day_start_balance")
            )
            daily_pnl = self._numeric(
                account_info.get("daily_realized_pnl")
                or account_info.get("daily_pnl")
                or metadata.get("daily_realized_pnl")
                or metadata.get("daily_pnl")
            )
        else:
            balance = self._numeric(getattr(account_info, "balance", None))
            equity = self._numeric(getattr(account_info, "equity", None))
            start_balance = self._numeric(
                getattr(account_info, "day_start_balance", None)
                or metadata.get("day_start_balance")
            )
            daily_pnl = self._numeric(
                getattr(account_info, "daily_realized_pnl", None)
                or getattr(account_info, "daily_pnl", None)
                or metadata.get("daily_realized_pnl")
                or metadata.get("daily_pnl")
            )

        if start_balance and daily_pnl is not None and start_balance > 0:
            loss_pct = max(0.0, (-daily_pnl / start_balance) * 100.0)
            return {
                "loss_pct": round(loss_pct, 4),
                "source": "daily_pnl_vs_day_start_balance",
                "day_start_balance": start_balance,
                "daily_pnl": daily_pnl,
            }

        if start_balance and equity is not None and start_balance > 0:
            loss_pct = max(0.0, ((start_balance - equity) / start_balance) * 100.0)
            return {
                "loss_pct": round(loss_pct, 4),
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
                action="block",
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
        sweep_confirmation_state = str(
            structure.get("sweep_confirmation_state") or "none"
        ).strip().lower()
        confirmation_reference = structure.get("confirmation_reference")

        if side == "buy" and sweep_confirmation_state.startswith("bearish_"):
            checks.append(
                RiskCheckResult(
                    name=self.name,
                    action="block",
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
                    action="block",
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
                        action="warn",
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
                        action="warn",
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
                action=action,
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
                action=action,
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
