from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional, Protocol

from src.config import EconomicConfig, get_economic_config

if TYPE_CHECKING:
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
        settings: Optional[EconomicConfig] = None,
    ) -> None:
        self.economic_calendar_service = economic_calendar_service
        self.settings = settings or get_economic_config()

    def is_enabled(self) -> bool:
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
        if self.economic_calendar_service is None:
            return {
                "stale": False,
                "provider_failure_threshold": 0,
                "degraded": False,
                "failing_providers": [],
                "provider_status": {},
            }

        stats = self.economic_calendar_service.stats()
        provider_status = stats.get("provider_status") or {}
        failure_threshold = max(0, int(self.settings.trade_guard_provider_failure_threshold))
        stale = bool(self.economic_calendar_service.is_stale())
        failing_providers = []

        if failure_threshold > 0:
            for provider, state in provider_status.items():
                if not bool(state.get("enabled", True)):
                    continue
                consecutive_failures = int(state.get("consecutive_failures") or 0)
                if consecutive_failures >= failure_threshold:
                    failing_providers.append(
                        {
                            "provider": provider,
                            "consecutive_failures": consecutive_failures,
                            "last_error": state.get("last_error"),
                        }
                    )

        degraded = stale or bool(failing_providers)
        return {
            "stale": stale,
            "provider_failure_threshold": failure_threshold,
            "degraded": degraded,
            "failing_providers": failing_providers,
            "provider_status": provider_status,
            "last_refresh_at": stats.get("last_refresh_at"),
            "refresh_in_progress": stats.get("refresh_in_progress"),
        }

    def assess_trade(
        self,
        *,
        symbol: str,
        at_time: Optional[datetime] = None,
        lookahead_minutes: Optional[int] = None,
        lookback_minutes: Optional[int] = None,
        importance_min: Optional[int] = None,
    ) -> Dict[str, Any]:
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
                "symbol": symbol,
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
            }
        assessment = self.economic_calendar_service.get_trade_guard(  # type: ignore[union-attr]
            symbol=symbol,
            at_time=at_time,
            lookahead_minutes=lookahead_minutes if lookahead_minutes is not None else self.settings.trade_guard_lookahead_minutes,
            lookback_minutes=lookback_minutes if lookback_minutes is not None else self.settings.trade_guard_lookback_minutes,
            importance_min=importance_min if importance_min is not None else self.settings.trade_guard_importance_min,
        )
        event_blocked = bool(assessment.get("blocked"))
        calendar_health = self._calendar_health_assessment()
        calendar_health_mode = self._calendar_health_mode()
        calendar_health_degraded = bool(calendar_health.get("degraded"))
        warnings = []
        reasons = []

        if event_blocked:
            active_windows = assessment.get("active_windows") or []
            if active_windows:
                reasons.append(f"High-impact economic event window active ({len(active_windows)} window(s))")
            else:
                reasons.append("High-impact economic event risk detected")

        if calendar_health_degraded:
            if calendar_health.get("stale"):
                warnings.append("Economic calendar data is stale")
            failing_providers = calendar_health.get("failing_providers") or []
            if failing_providers:
                failing_names = ", ".join(item["provider"] for item in failing_providers)
                warnings.append(f"Economic calendar providers degraded: {failing_names}")
            reasons.extend(warnings)

        blocked = bool(event_blocked and mode == "block")
        if not blocked and calendar_health_degraded and calendar_health_mode == "fail_closed":
            blocked = True

        if blocked:
            action = "block"
        elif event_blocked or (calendar_health_degraded and calendar_health_mode == "warn_only"):
            action = "warn"
        else:
            action = "allow"

        reason = "; ".join(dict.fromkeys(reasons)) if reasons else None

        return {
            **assessment,
            "enabled": True,
            "mode": mode,
            "event_blocked": event_blocked,
            "calendar_health_degraded": calendar_health_degraded,
            "action": action,
            "blocked": blocked,
            "reason": reason,
            "warnings": warnings,
            "calendar_health_mode": calendar_health_mode,
            "calendar_health": calendar_health,
        }

    def enforce_trade_allowed(
        self,
        *,
        symbol: str,
        at_time: Optional[datetime] = None,
        lookahead_minutes: Optional[int] = None,
        lookback_minutes: Optional[int] = None,
        importance_min: Optional[int] = None,
    ) -> Dict[str, Any]:
        assessment = self.assess_trade(
            symbol=symbol,
            at_time=at_time,
            lookahead_minutes=lookahead_minutes,
            lookback_minutes=lookback_minutes,
            importance_min=importance_min,
        )
        if assessment["action"] == "block":
            raise PreTradeRiskBlockedError(
                assessment.get("reason") or "Trade blocked by pre-trade risk policy",
                assessment=assessment,
            )
        return assessment
