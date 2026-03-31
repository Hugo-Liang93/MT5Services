"""Signal-domain pre-evaluation filters for SignalRuntime.

These filters only decide whether a *signal* should be evaluated/emitted
under current market context (session, spread, economic window).
They are NOT account/portfolio risk controls and must not be treated as
final trade safety gates.

Final trade risk control is enforced by `src.risk.service`
before order dispatch.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
from typing import Any, Dict, List, Optional, Protocol

from ..contracts import (
    SESSION_ASIA,
    SESSION_LONDON,
    SESSION_NEW_YORK,
    SESSION_OFF_HOURS,
    normalize_session_name,
    resolve_session_by_hour,
)


class TradeGuardProvider(Protocol):
    def get_trade_guard(self, **kwargs: Any) -> Dict[str, Any]: ...


@dataclass
class SessionFilter:
    """Identify active trading sessions and filter by allowed sessions."""

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
        current = utc_now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        else:
            current = current.astimezone(timezone.utc)
        session_name = resolve_session_by_hour(current.hour)
        if session_name in {
            SESSION_ASIA,
            SESSION_LONDON,
            SESSION_NEW_YORK,
            SESSION_OFF_HOURS,
        }:
            return [session_name]
        return [SESSION_OFF_HOURS]

    def is_active_session(self, utc_now: Optional[datetime] = None) -> bool:
        if not self.allowed_sessions:
            return True
        current = self.current_sessions(utc_now)
        return any(s in self.allowed_sessions for s in current)


@dataclass
class SpreadFilter:
    """Reject signals when the bid-ask spread is too wide."""

    max_spread_points: float = 50.0
    session_max_spread_points: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized: dict[str, float] = {}
        for session_name, max_points in self.session_max_spread_points.items():
            normalized[normalize_session_name(session_name)] = float(max_points)
        self.session_max_spread_points = normalized

    def threshold_for_sessions(self, sessions: List[str] | None = None) -> float:
        threshold = float(self.max_spread_points)
        for session_name in sessions or []:
            if session_name in self.session_max_spread_points:
                threshold = min(threshold, self.session_max_spread_points[session_name])
        return threshold

    def is_spread_acceptable(
        self,
        spread_points: float,
        sessions: List[str] | None = None,
    ) -> bool:
        threshold = self.threshold_for_sessions(sessions)
        if threshold <= 0:
            return True
        return spread_points <= threshold


@dataclass
class EconomicEventFilter:
    """分级经济事件过滤器。

    - importance >= block_importance_min（默认3）→ 阻断交易
    - importance < block_importance_min           → 仅警告，不阻断
    """

    provider: Optional[TradeGuardProvider] = None
    lookahead_minutes: int = 30
    lookback_minutes: int = 15
    importance_min: int = 2

    def check_trade_guard(self, symbol: str, utc_now: Optional[datetime] = None) -> tuple[bool, str]:
        """返回 (safe, reason)。safe=True 时可交易。"""
        if self.provider is None:
            return True, ""
        at_time = utc_now or datetime.now(timezone.utc)
        try:
            guard = self.provider.get_trade_guard(
                symbol=symbol,
                at_time=at_time,
                lookahead_minutes=self.lookahead_minutes,
                lookback_minutes=self.lookback_minutes,
                importance_min=self.importance_min,
            )
            severity = guard.get("severity", "none")
            if severity == "block":
                return False, "economic_event_block"
            if severity == "warn":
                return True, "economic_event_warn"
            return True, ""
        except Exception as exc:
            logger.warning("Trade guard check failed for %s: %s", symbol, exc)
            return True, ""

    def is_safe_to_trade(self, symbol: str, utc_now: Optional[datetime] = None) -> bool:
        safe, _ = self.check_trade_guard(symbol, utc_now)
        return safe


@dataclass
class SessionTransitionFilter:
    """Suppress signal evaluation around major session handoff windows."""

    cooldown_minutes: int = 15
    transition_schedule_utc: dict[str, int] = field(
        default_factory=lambda: {"london_to_new_york": 13 * 60}
    )

    def active_transition(self, utc_now: Optional[datetime] = None) -> Optional[str]:
        if self.cooldown_minutes <= 0:
            return None
        current = utc_now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        else:
            current = current.astimezone(timezone.utc)
        current_minute = (
            current.hour * 60
            + current.minute
            + (current.second / 60.0)
        )
        for name, center_minute in self.transition_schedule_utc.items():
            if abs(current_minute - float(center_minute)) <= float(
                self.cooldown_minutes
            ):
                return name
        return None

    def is_safe(self, utc_now: Optional[datetime] = None) -> bool:
        return self.active_transition(utc_now) is None


@dataclass
class VolatilitySpikeFilter:
    """Suppress signal evaluation when ATR spikes above a baseline multiple."""

    spike_multiplier: float = 0.0  # 0 = disabled

    def is_volatility_acceptable(
        self,
        indicators: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if self.spike_multiplier <= 0 or not indicators:
            return True
        atr_data = indicators.get("atr14")
        if not isinstance(atr_data, dict):
            return True
        current_atr = atr_data.get("atr")
        baseline_atr = atr_data.get("atr_sma") or atr_data.get("atr")
        if current_atr is None or baseline_atr is None:
            return True
        # NaN/Inf 防御：异常数值视为波动率不可信，放行由下游决策
        if not math.isfinite(current_atr) or not math.isfinite(baseline_atr):
            return True
        if baseline_atr > 0 and current_atr > baseline_atr * self.spike_multiplier:
            return False
        return True


@dataclass
class SignalFilterChain:
    """Composite filter that runs all pre-evaluation checks."""

    session_filter: Optional[SessionFilter] = None
    session_transition_filter: Optional[SessionTransitionFilter] = None
    spread_filter: Optional[SpreadFilter] = None
    economic_filter: Optional[EconomicEventFilter] = None
    volatility_filter: Optional[VolatilitySpikeFilter] = None

    def should_evaluate(
        self,
        symbol: str,
        *,
        spread_points: float = 0.0,
        utc_now: Optional[datetime] = None,
        active_sessions: Optional[List[str]] = None,
        indicators: Optional[Dict[str, Any]] = None,
    ) -> tuple[bool, str]:
        """Return (allowed, reason). reason is empty when allowed."""
        sessions = active_sessions
        if sessions is None and self.session_filter:
            sessions = self.session_filter.current_sessions(utc_now)

        # 过滤器按 cheapest-first 排列：先做 O(1) 检查，I/O 最贵的放最后
        if self.session_filter and not self.session_filter.is_active_session(utc_now):
            sessions = sessions or self.session_filter.current_sessions(utc_now)
            return False, f"outside_allowed_sessions:{','.join(sessions)}"

        if self.spread_filter and not self.spread_filter.is_spread_acceptable(
            spread_points,
            sessions=sessions,
        ):
            threshold = self.spread_filter.threshold_for_sessions(sessions)
            session_label = ",".join(sessions or []) or "unknown"
            return (
                False,
                f"spread_too_wide:{spread_points:.1f}>{threshold:.1f}[{session_label}]",
            )

        if (
            self.volatility_filter
            and not self.volatility_filter.is_volatility_acceptable(indicators)
        ):
            return False, "volatility_spike"

        if (
            self.session_transition_filter
            and not self.session_transition_filter.is_safe(utc_now)
        ):
            transition_name = self.session_transition_filter.active_transition(utc_now)
            return False, f"session_transition_cooldown:{transition_name}"

        if self.economic_filter:
            safe, reason = self.economic_filter.check_trade_guard(symbol, utc_now)
            if not safe:
                return False, reason
            # reason == "economic_event_warn" → 允许交易但日志记录（由调用方处理）

        return True, ""

    def filter_status(
        self,
        symbol: str = "XAUUSD",
        *,
        utc_now: Optional[datetime] = None,
    ) -> dict[str, dict[str, Any]]:
        """返回每个过滤规则的当前实时状态，供 Studio 前端展示。"""
        result: dict[str, dict[str, Any]] = {}

        if self.session_filter:
            sessions = self.session_filter.current_sessions(utc_now)
            active = self.session_filter.is_active_session(utc_now)
            result["session"] = {
                "active": active,
                "current_sessions": sessions,
                "allowed_sessions": list(self.session_filter.allowed_sessions),
            }

        if self.session_transition_filter:
            transition = self.session_transition_filter.active_transition(utc_now)
            result["session_transition"] = {
                "active": transition is None,
                "in_cooldown": transition is not None,
                "transition_name": transition,
                "cooldown_minutes": self.session_transition_filter.cooldown_minutes,
            }

        if self.volatility_filter:
            result["volatility"] = {
                "active": True,
                "spike_multiplier": self.volatility_filter.spike_multiplier,
                "enabled": self.volatility_filter.spike_multiplier > 0,
            }

        if self.spread_filter:
            result["spread"] = {
                "active": True,
                "enabled": True,
            }

        if self.economic_filter:
            safe, reason = self.economic_filter.check_trade_guard(symbol, utc_now)
            result["economic"] = {
                "active": safe,
                "blocked": not safe,
                "reason": reason if not safe else "",
            }

        return result
