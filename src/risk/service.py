from __future__ import annotations

"""Account/portfolio-level pre-trade risk service.

`src.risk` is the final trade safety gate before order execution.
It validates intent against account state, protection rules and
economic calendar guard policies.

This layer is intentionally different from `src.signals.filters`:
- signals.filters: signal-domain gating (whether to evaluate/emit signal)
- risk.service: order-domain gating (whether a trade is allowed)
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional, Protocol

from src.config import EconomicConfig, RiskConfig, get_economic_config, get_risk_config

from .models import RiskAssessment, RiskCheckResult, TradeIntent
from .rules import (
    AccountSnapshotRule,
    CalendarHealthRule,
    EconomicEventRule,
    ProtectionRule,
    RuleContext,
)

if TYPE_CHECKING:
    from src.core.account_service import AccountService
    from src.core.economic_calendar_service import EconomicCalendarService


class EconomicCalendarProvider(Protocol):
    def is_stale(self) -> bool:
        ...

    def stats(self) -> Dict[str, Any]:
        ...

    def get_trade_guard(
        self,
        *,
        symbol: str,
        at_time: Optional[datetime] = None,
        lookahead_minutes: int = 180,
        lookback_minutes: int = 0,
        importance_min: Optional[int] = None,
    ) -> Dict[str, Any]:
        ...


class PreTradeRiskBlockedError(RuntimeError):
    def __init__(self, message: str, assessment: Dict[str, Any]):
        super().__init__(message)
        self.assessment = assessment


class PreTradeRiskService:
    def __init__(
        self,
        economic_calendar_service: Optional["EconomicCalendarService | EconomicCalendarProvider"] = None,
        account_service: Optional["AccountService"] = None,
        settings: Optional[EconomicConfig] = None,
        risk_settings: Optional[RiskConfig] = None,
    ) -> None:
        self.economic_calendar_service = economic_calendar_service
        self.account_service = account_service
        self.settings = settings or get_economic_config()
        self.risk_settings = risk_settings or get_risk_config()
        self.rules = (
            AccountSnapshotRule(),
            ProtectionRule(),
            EconomicEventRule(),
            CalendarHealthRule(),
        )

    def is_enabled(self) -> bool:
        return self._economic_guard_enabled() or bool(self.risk_settings.enabled)

    def _economic_guard_enabled(self) -> bool:
        return bool(
            self.settings.enabled
            and self.settings.trade_guard_enabled
            and self.economic_calendar_service is not None
        )

    def _effective_mode(self) -> str:
        mode = str(self.settings.trade_guard_mode or "warn_only").strip().lower()
        return mode if mode in {"off", "warn_only", "block"} else "warn_only"

    def _calendar_health_mode(self) -> str:
        mode = str(self.settings.trade_guard_calendar_health_mode or "warn_only").strip().lower()
        return mode if mode in {"fail_open", "warn_only", "fail_closed"} else "warn_only"

    def _calendar_health_assessment(self) -> Dict[str, Any]:
        for rule in self.rules:
            if isinstance(rule, CalendarHealthRule):
                checks = rule.evaluate(
                    RuleContext(
                        intent=TradeIntent(symbol="", volume=0.0, side="buy"),
                        economic_settings=self.settings,
                        risk_settings=self.risk_settings,
                        economic_provider=self.economic_calendar_service,
                        account_provider=self.account_service,
                        mode=self._effective_mode(),
                        calendar_health_mode=self._calendar_health_mode(),
                    )
                )
                if not checks:
                    return {
                        "stale": False,
                        "provider_failure_threshold": max(0, int(self.settings.trade_guard_provider_failure_threshold)),
                        "degraded": False,
                        "failing_providers": [],
                        "provider_status": {},
                    }
                details = checks[0].details
                return {
                    "stale": bool(details.get("stale")),
                    "provider_failure_threshold": details.get("provider_failure_threshold", 0),
                    "degraded": True,
                    "failing_providers": details.get("failing_providers", []),
                    "provider_status": details.get("provider_status", {}),
                    "last_refresh_at": details.get("last_refresh_at"),
                    "refresh_in_progress": details.get("refresh_in_progress"),
                }
        return {
            "stale": False,
            "provider_failure_threshold": 0,
            "degraded": False,
            "failing_providers": [],
            "provider_status": {},
        }

    def _build_intent(
        self,
        *,
        intent: Optional[TradeIntent] = None,
        symbol: Optional[str] = None,
        volume: Optional[float] = None,
        side: Optional[str] = None,
        order_kind: str = "market",
        price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        deviation: int = 20,
        comment: str = "",
        magic: int = 0,
        at_time: Optional[datetime] = None,
    ) -> TradeIntent:
        if intent is not None:
            return intent
        if not symbol:
            raise ValueError("symbol is required for risk assessment")
        return TradeIntent(
            symbol=symbol,
            volume=float(volume or 0.0),
            side=str(side or "buy"),
            order_kind=str(order_kind or "market"),
            price=price,
            sl=sl,
            tp=tp,
            deviation=deviation,
            comment=comment,
            magic=magic,
            at_time=at_time,
        )

    def assess_trade(
        self,
        *,
        symbol: Optional[str] = None,
        volume: Optional[float] = None,
        side: Optional[str] = None,
        order_kind: str = "market",
        price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        deviation: int = 20,
        comment: str = "",
        magic: int = 0,
        intent: Optional[TradeIntent] = None,
        at_time: Optional[datetime] = None,
        lookahead_minutes: Optional[int] = None,
        lookback_minutes: Optional[int] = None,
        importance_min: Optional[int] = None,
    ) -> Dict[str, Any]:
        intent_obj = self._build_intent(
            intent=intent,
            symbol=symbol,
            volume=volume,
            side=side,
            order_kind=order_kind,
            price=price,
            sl=sl,
            tp=tp,
            deviation=deviation,
            comment=comment,
            magic=magic,
            at_time=at_time,
        )
        mode = self._effective_mode()
        if not self.is_enabled() or mode == "off":
            return {
                "enabled": False,
                "mode": mode,
                "event_blocked": False,
                "calendar_health_degraded": False,
                "blocked": False,
                "action": "allow",
                "reason": None,
                "symbol": intent_obj.symbol,
                "active_windows": [],
                "upcoming_windows": [],
                "warnings": [],
                "calendar_health": {
                    "stale": False,
                    "provider_failure_threshold": 0,
                    "degraded": False,
                    "failing_providers": [],
                    "provider_status": {},
                },
                "checks": [],
                "intent": intent_obj.to_dict(),
            }

        base_assessment: Dict[str, Any]
        if self._economic_guard_enabled():
            base_assessment = self.economic_calendar_service.get_trade_guard(  # type: ignore[union-attr]
                symbol=intent_obj.symbol,
                at_time=intent_obj.at_time,
                lookahead_minutes=lookahead_minutes if lookahead_minutes is not None else self.settings.trade_guard_lookahead_minutes,
                lookback_minutes=lookback_minutes if lookback_minutes is not None else self.settings.trade_guard_lookback_minutes,
                importance_min=importance_min if importance_min is not None else self.settings.trade_guard_importance_min,
            )
        else:
            base_assessment = {
                "symbol": intent_obj.symbol,
                "blocked": False,
                "active_windows": [],
                "upcoming_windows": [],
                "currencies": [],
                "countries": [],
            }

        context = RuleContext(
            intent=intent_obj,
            economic_settings=self.settings,
            risk_settings=self.risk_settings,
            economic_provider=self.economic_calendar_service,
            account_provider=self.account_service,
            mode=mode,
            calendar_health_mode=self._calendar_health_mode(),
        )

        checks: list[RiskCheckResult] = []
        for rule in self.rules:
            checks.extend(rule.evaluate(context))

        warnings = [check.reason for check in checks if check.action == "warn" and check.reason]
        reasons = [check.reason for check in checks if check.action == "block" and check.reason]
        action = "block" if any(check.action == "block" for check in checks) else (
            "warn" if any(check.action == "warn" for check in checks) else "allow"
        )
        assessment_obj = RiskAssessment(
            action=action,
            blocked=action == "block",
            reason="; ".join(dict.fromkeys(reasons)) if reasons else None,
            warnings=list(dict.fromkeys(warnings)),
            checks=checks,
        )
        calendar_health = self._calendar_health_assessment()

        return {
            **base_assessment,
            "enabled": True,
            "mode": mode,
            "event_blocked": bool(base_assessment.get("blocked")),
            "calendar_health_degraded": bool(calendar_health.get("degraded")),
            "action": assessment_obj.action,
            "blocked": assessment_obj.blocked,
            "reason": assessment_obj.reason,
            "warnings": assessment_obj.warnings,
            "calendar_health_mode": self._calendar_health_mode(),
            "calendar_health": calendar_health,
            "checks": [check.to_dict() for check in checks],
            "intent": intent_obj.to_dict(),
        }

    def enforce_trade_allowed(self, **kwargs: Any) -> Dict[str, Any]:
        assessment = self.assess_trade(**kwargs)
        if assessment["action"] == "block":
            raise PreTradeRiskBlockedError(
                assessment.get("reason") or "Trade blocked by pre-trade risk policy",
                assessment=assessment,
            )
        return assessment
