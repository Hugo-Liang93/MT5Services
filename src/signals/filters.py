"""Signal-domain pre-evaluation filters for SignalRuntime.

These filters only decide whether a *signal* should be evaluated/emitted
under current market context (session, spread, economic window).
They are NOT account/portfolio risk controls and must not be treated as
final trade safety gates.

Final trade risk control is enforced by `src.risk` / `src.core.pretrade_risk_service`
before order dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Any, Dict, List, Optional, Protocol

from .contracts import (
    SESSION_ASIA,
    SESSION_LONDON,
    SESSION_NEW_YORK,
    SESSION_OFF_HOURS,
    normalize_session_name,
)


class TradeGuardProvider(Protocol):
    def get_trade_guard(self, **kwargs: Any) -> Dict[str, Any]: ...


@dataclass
class SessionFilter:
    """Identify active trading sessions and filter by allowed sessions."""

    london_open: time = time(8, 0)
    london_close: time = time(16, 0)
    ny_open: time = time(13, 0)
    ny_close: time = time(21, 0)
    asia_open: time = time(0, 0)
    asia_close: time = time(8, 0)

    allowed_sessions: tuple[str, ...] = (SESSION_LONDON, SESSION_NEW_YORK)

    def __post_init__(self) -> None:
        normalized = tuple(
            normalize_session_name(name)
            for name in self.allowed_sessions
            if str(name).strip()
        )
        valid = {SESSION_ASIA, SESSION_LONDON, SESSION_NEW_YORK, SESSION_OFF_HOURS}
        invalid = [name for name in normalized if name not in valid]
        if invalid:
            raise ValueError(f"unsupported session names: {invalid}")
        self.allowed_sessions = normalized

    def current_sessions(self, utc_now: Optional[datetime] = None) -> List[str]:
        t = (utc_now or datetime.now(timezone.utc)).time()
        sessions: List[str] = []
        if self.asia_open <= t < self.asia_close:
            sessions.append(SESSION_ASIA)
        if self.london_open <= t < self.london_close:
            sessions.append(SESSION_LONDON)
        if self.ny_open <= t < self.ny_close:
            sessions.append(SESSION_NEW_YORK)
        return sessions or [SESSION_OFF_HOURS]

    def is_active_session(self, utc_now: Optional[datetime] = None) -> bool:
        if not self.allowed_sessions:
            return True
        current = self.current_sessions(utc_now)
        return any(s in self.allowed_sessions for s in current)


@dataclass
class SpreadFilter:
    """Reject signals when the bid-ask spread is too wide."""

    max_spread_points: float = 50.0

    def is_spread_acceptable(self, spread_points: float) -> bool:
        if self.max_spread_points <= 0:
            return True
        return spread_points <= self.max_spread_points


@dataclass
class EconomicEventFilter:
    """Suppress signals during high-impact economic event windows."""

    provider: Optional[TradeGuardProvider] = None
    lookahead_minutes: int = 30
    lookback_minutes: int = 15
    importance_min: int = 3

    def is_safe_to_trade(self, symbol: str, utc_now: Optional[datetime] = None) -> bool:
        if self.provider is None:
            return True
        at_time = utc_now or datetime.now(timezone.utc)
        try:
            guard = self.provider.get_trade_guard(
                symbol=symbol,
                at_time=at_time,
                lookahead_minutes=self.lookahead_minutes,
                lookback_minutes=self.lookback_minutes,
                importance_min=self.importance_min,
            )
            return not bool(guard.get("blocked"))
        except Exception:
            return True


@dataclass
class SignalFilterChain:
    """Composite filter that runs all pre-evaluation checks."""

    session_filter: Optional[SessionFilter] = None
    spread_filter: Optional[SpreadFilter] = None
    economic_filter: Optional[EconomicEventFilter] = None

    def should_evaluate(
        self,
        symbol: str,
        *,
        spread_points: float = 0.0,
        utc_now: Optional[datetime] = None,
    ) -> tuple[bool, str]:
        """Return (allowed, reason). reason is empty when allowed."""
        if self.session_filter and not self.session_filter.is_active_session(utc_now):
            sessions = self.session_filter.current_sessions(utc_now)
            return False, f"outside_allowed_sessions:{','.join(sessions)}"

        if self.spread_filter and not self.spread_filter.is_spread_acceptable(
            spread_points
        ):
            return (
                False,
                f"spread_too_wide:{spread_points:.1f}>{self.spread_filter.max_spread_points}",
            )

        if self.economic_filter and not self.economic_filter.is_safe_to_trade(
            symbol, utc_now
        ):
            return False, "economic_event_window"

        return True, ""
