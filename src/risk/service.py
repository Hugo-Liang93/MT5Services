from __future__ import annotations

"""Account/portfolio-level pre-trade risk service.

`src.risk` is the final trade safety gate before order execution.
It validates intent against account state, protection rules and
economic calendar guard policies.

This layer is intentionally different from `src.signals.filters`:
- signals.filters: signal-domain gating (whether to evaluate/emit signal)
- risk.service: order-domain gating (whether a trade is allowed)
"""

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional, Protocol

logger = logging.getLogger(__name__)

TRADE_FREQUENCY_RESERVATION_ID = "trade_frequency_reservation_id"

from src.config import EconomicConfig, RiskConfig, get_economic_config, get_risk_config

from .models import RiskAssessment, RiskCheckResult, TradeIntent
from .profiles import RiskProfileResolutionError, RiskProfileResolver, RiskProfileSelection
from .rules import (
    AccountSnapshotRule,
    AccountStateProvider,
    CalendarHealthRule,
    DailyLossLimitRule,
    EconomicEventRule,
    MarginAvailabilityRule,
    MarketStructureRule,
    ProtectionRule,
    RuleContext,
    SessionWindowRule,
    TradeFrequencyProvider,
    TradeFrequencyQuotaExceeded,
    TradeFrequencyRule,
)

if TYPE_CHECKING:
    from src.calendar.service import EconomicCalendarService


_RULE_NAMES_BY_TYPE: tuple[tuple[type, str], ...] = (
    (AccountSnapshotRule, "account_snapshot"),
    (DailyLossLimitRule, "daily_loss_limit"),
    (MarginAvailabilityRule, "margin_availability"),
    (TradeFrequencyRule, "trade_frequency"),
    (ProtectionRule, "protection"),
    (SessionWindowRule, "session_window"),
    (MarketStructureRule, "market_structure"),
    (EconomicEventRule, "economic_event"),
    (CalendarHealthRule, "calendar_health"),
)


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


def resolve_risk_failure_key(assessment: Dict[str, Any] | None) -> str | None:
    checks = list((assessment or {}).get("checks") or [])
    for item in checks:
        verdict = str(item.get("verdict") or "").strip().lower()
        if verdict != "block":
            continue
        rule_name = str(item.get("name") or "").strip()
        if rule_name:
            return rule_name
    for item in checks:
        rule_name = str(item.get("name") or "").strip()
        if rule_name:
            return rule_name
    reason = str((assessment or {}).get("reason") or "").strip().lower()
    if reason == "daily_loss_limit_reached":
        return "daily_loss_limit"
    return None


class PreTradeRiskService:
    def __init__(
        self,
        economic_calendar_service: Optional["EconomicCalendarService | EconomicCalendarProvider"] = None,
        account_service: Optional[AccountStateProvider] = None,
        settings: Optional[EconomicConfig] = None,
        risk_settings: Optional[RiskConfig] = None,
        trade_frequency_provider: Optional[TradeFrequencyProvider] = None,
        account_key: Optional[str] = None,
    ) -> None:
        self.economic_calendar_service = economic_calendar_service
        self.account_service = account_service
        self.settings = settings or get_economic_config()
        self.risk_settings = risk_settings or get_risk_config()
        self.trade_frequency_provider = trade_frequency_provider
        self.account_key = str(account_key or "").strip() or None
        self._trade_frequency_rule = TradeFrequencyRule()
        self._risk_profile_resolver = RiskProfileResolver(self.risk_settings)
        # Rule execution order: fast-fail first (cheapest checks early),
        # then progressively more expensive / external checks.
        # 1. AccountSnapshot — in-memory account state (max positions, equity)
        # 2. DailyLossLimit — equity vs balance comparison
        # 3. MarginAvailability — margin calculation (may call MT5)
        # 4. TradeFrequency — in-memory cooldown counter
        # 5. Protection — SL/TP validation (pure arithmetic)
        # 6. SessionWindow — time-based session check
        # 7. MarketStructure — market structure context (cached)
        # 8. EconomicEvent — economic calendar lookup (cached, may query DB)
        # 9. CalendarHealth — calendar service health check
        self.rules = (
            AccountSnapshotRule(),
            DailyLossLimitRule(),
            MarginAvailabilityRule(),
            self._trade_frequency_rule,
            ProtectionRule(),
            SessionWindowRule(),
            MarketStructureRule(),
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
                        trade_frequency_provider=self.trade_frequency_provider,
                        account_key=self.account_key,
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
        metadata: Optional[Dict[str, Any]] = None,
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
            metadata=dict(metadata or {}),
        )

    def _resolve_account_key(self, intent: TradeIntent) -> str | None:
        metadata = intent.metadata if isinstance(intent.metadata, dict) else {}
        for key in ("account_key", "target_account_key"):
            value = str(metadata.get(key) or "").strip()
            if value:
                return value
        return self.account_key

    def _resolve_account_key_from_assessment(
        self,
        assessment: Dict[str, Any],
    ) -> str | None:
        intent = assessment.get("intent") if isinstance(assessment, dict) else {}
        metadata = intent.get("metadata") if isinstance(intent, dict) else {}
        if isinstance(metadata, dict):
            for key in ("account_key", "target_account_key"):
                value = str(metadata.get(key) or "").strip()
                if value:
                    return value
        return self.account_key

    @staticmethod
    def _optional_positive_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    def _frequency_limits(self) -> tuple[int | None, int | None]:
        return (
            self._optional_positive_int(
                getattr(self.risk_settings, "max_trades_per_day", None)
            ),
            self._optional_positive_int(
                getattr(self.risk_settings, "max_trades_per_hour", None)
            ),
        )

    @staticmethod
    def _trade_frequency_enabled(
        profile: RiskProfileSelection | Dict[str, Any] | None,
    ) -> bool:
        if profile is None:
            return True
        if isinstance(profile, RiskProfileSelection):
            return profile.trade_frequency_enabled
        return bool(profile.get("trade_frequency_enabled", True))

    @staticmethod
    def _rule_name(rule: Any) -> str | None:
        for rule_type, rule_name in _RULE_NAMES_BY_TYPE:
            if isinstance(rule, rule_type):
                return rule_name
        return None

    @staticmethod
    def _profile_enables_rule(
        profile: RiskProfileSelection,
        rule_name: str | None,
    ) -> bool:
        if not rule_name:
            return True
        return profile.enables_rule(rule_name)

    def _resolve_risk_profile(self, intent: TradeIntent) -> RiskProfileSelection:
        return self._risk_profile_resolver.resolve(intent)

    def _risk_data_fail_closed(self) -> bool:
        return (
            str(
                getattr(self.risk_settings, "data_unavailable_policy", "warn_only")
                or "warn_only"
            )
            .strip()
            .lower()
            == "fail_closed"
        )

    def _append_reservation_warning(
        self,
        assessment: Dict[str, Any],
        reason: str,
    ) -> None:
        warnings = list(assessment.get("warnings") or [])
        warnings.append(reason)
        assessment["warnings"] = list(dict.fromkeys(warnings))
        checks = list(assessment.get("checks") or [])
        checks.append(
            RiskCheckResult(
                name="trade_frequency_reservation",
                verdict="warn",
                reason=reason,
            ).to_dict()
        )
        assessment["checks"] = checks

    def _reservation_blocked_assessment(
        self,
        assessment: Dict[str, Any],
        reason: str,
    ) -> Dict[str, Any]:
        checks = list(assessment.get("checks") or [])
        checks.append(
            RiskCheckResult(
                name="trade_frequency_reservation",
                verdict="block",
                reason=reason,
            ).to_dict()
        )
        return {
            **assessment,
            "verdict": "block",
            "blocked": True,
            "reason": reason,
            "checks": checks,
        }

    def _reserve_trade_frequency_slot(self, assessment: Dict[str, Any]) -> None:
        if not self._trade_frequency_enabled(assessment.get("risk_profile")):
            return
        max_per_day, max_per_hour = self._frequency_limits()
        if max_per_day is None and max_per_hour is None:
            return
        provider = self.trade_frequency_provider
        if provider is None:
            return
        reserve = getattr(provider, "reserve_trade_slot", None)
        if reserve is None:
            reason = "Trade frequency reservation port unavailable"
            if self._risk_data_fail_closed():
                blocked = self._reservation_blocked_assessment(assessment, reason)
                raise PreTradeRiskBlockedError(reason, assessment=blocked)
            self._append_reservation_warning(assessment, reason)
            return
        account_key = self._resolve_account_key_from_assessment(assessment)
        if not account_key:
            reason = "Trade frequency reservation requires account_key"
            if self._risk_data_fail_closed():
                blocked = self._reservation_blocked_assessment(assessment, reason)
                raise PreTradeRiskBlockedError(reason, assessment=blocked)
            self._append_reservation_warning(assessment, reason)
            return
        try:
            reservation_id = reserve(
                account_key=account_key,
                at_time=datetime.now(timezone.utc),
                max_trades_per_day=max_per_day,
                max_trades_per_hour=max_per_hour,
            )
        except TradeFrequencyQuotaExceeded as exc:
            reason = str(exc) or "Trade frequency limit reached"
            blocked = self._reservation_blocked_assessment(assessment, reason)
            raise PreTradeRiskBlockedError(reason, assessment=blocked) from exc
        except Exception as exc:
            reason = f"Trade frequency reservation unavailable: {exc}"
            if self._risk_data_fail_closed():
                blocked = self._reservation_blocked_assessment(assessment, reason)
                raise PreTradeRiskBlockedError(reason, assessment=blocked)
            self._append_reservation_warning(assessment, reason)
            return
        if reservation_id:
            assessment[TRADE_FREQUENCY_RESERVATION_ID] = str(reservation_id)

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
        metadata: Optional[Dict[str, Any]] = None,
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
            metadata=metadata,
        )
        try:
            risk_profile = self._resolve_risk_profile(intent_obj)
            risk_profile_payload = risk_profile.to_dict()
        except RiskProfileResolutionError as exc:
            check = RiskCheckResult(
                name="risk_profile",
                verdict="block",
                reason=str(exc),
            )
            return {
                "enabled": bool(self.is_enabled()),
                "mode": self._effective_mode(),
                "event_blocked": False,
                "calendar_health_degraded": False,
                "blocked": True,
                "verdict": "block",
                "reason": str(exc),
                "symbol": intent_obj.symbol,
                "active_windows": [],
                "upcoming_windows": [],
                "warnings": [],
                "calendar_health": self._calendar_health_assessment(),
                "checks": [check.to_dict()],
                "intent": intent_obj.to_dict(),
                "risk_profile": {
                    "name": None,
                    "policy": None,
                    "trade_frequency_enabled": None,
                    "source": "resolution_error",
                },
            }
        mode = self._effective_mode()
        if not self.is_enabled() or mode == "off":
            return {
                "enabled": False,
                "mode": mode,
                "event_blocked": False,
                "calendar_health_degraded": False,
                "blocked": False,
                "verdict": "allow",
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
                "risk_profile": risk_profile_payload,
            }

        base_assessment: Dict[str, Any]
        if self._economic_guard_enabled():
            economic_calendar_service = self.economic_calendar_service
            assert economic_calendar_service is not None
            base_assessment = economic_calendar_service.get_trade_guard(
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
            trade_frequency_provider=self.trade_frequency_provider,
            account_key=self._resolve_account_key(intent_obj),
            mode=mode,
            calendar_health_mode=self._calendar_health_mode(),
        )

        checks: list[RiskCheckResult] = []
        for rule in self.rules:
            if not self._profile_enables_rule(risk_profile, self._rule_name(rule)):
                continue
            rule_checks = rule.evaluate(context)
            checks.extend(rule_checks)
            for check in rule_checks:
                if check.verdict == "block":
                    logger.warning(
                        "Risk BLOCK [%s] %s %s %s: %s",
                        check.name,
                        intent_obj.symbol,
                        intent_obj.side,
                        intent_obj.volume,
                        check.reason,
                    )

        warnings = [check.reason for check in checks if check.verdict == "warn" and check.reason]
        reasons = [check.reason for check in checks if check.verdict == "block" and check.reason]
        verdict = "block" if any(check.verdict == "block" for check in checks) else (
            "warn" if any(check.verdict == "warn" for check in checks) else "allow"
        )
        assessment_obj = RiskAssessment(
            verdict=verdict,
            blocked=verdict == "block",
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
            "verdict": assessment_obj.verdict,
            "blocked": assessment_obj.blocked,
            "reason": assessment_obj.reason,
            "warnings": assessment_obj.warnings,
            "calendar_health_mode": self._calendar_health_mode(),
            "calendar_health": calendar_health,
            "checks": [check.to_dict() for check in checks],
            "intent": intent_obj.to_dict(),
            "risk_profile": risk_profile_payload,
        }

    def record_trade_execution(self, at: Optional[datetime] = None) -> None:
        """Notify the frequency rule that a trade was executed."""
        if self.trade_frequency_provider is not None:
            return
        self._trade_frequency_rule.record_trade(at)

    def finalize_trade_frequency_reservation(
        self,
        assessment: Dict[str, Any] | None,
        *,
        committed: bool,
    ) -> None:
        if not assessment:
            return
        reservation_id = str(
            assessment.get(TRADE_FREQUENCY_RESERVATION_ID) or ""
        ).strip()
        if not reservation_id or self.trade_frequency_provider is None:
            return
        finalize = getattr(self.trade_frequency_provider, "finalize_trade_slot", None)
        if finalize is None:
            return
        finalize(reservation_id, committed=committed)

    def enforce_trade_allowed(self, **kwargs: Any) -> Dict[str, Any]:
        assessment = self.assess_trade(**kwargs)
        if assessment["verdict"] == "block":
            raise PreTradeRiskBlockedError(
                assessment.get("reason") or "Trade blocked by pre-trade risk policy",
                assessment=assessment,
            )
        self._reserve_trade_frequency_slot(assessment)
        return assessment
