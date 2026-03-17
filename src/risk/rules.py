from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

from src.config import EconomicConfig, RiskConfig

from .models import RiskCheckResult, TradeIntent


class EconomicCalendarRuleProvider(Protocol):
    def is_stale(self) -> bool:
        ...

    def stats(self) -> Dict[str, Any]:
        ...

    def get_trade_guard(self, **kwargs: Any) -> Dict[str, Any]:
        ...


class AccountStateProvider(Protocol):
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

