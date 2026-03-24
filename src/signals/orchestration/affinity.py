"""Regime affinity logic extracted from SignalRuntime.

Pure functions for:
- Computing effective affinity (hard / soft regime)
- Fast-reject eligibility check across all strategies
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..evaluation.regime import RegimeType, SoftRegimeResult

logger = logging.getLogger(__name__)


def effective_affinity(
    strategy: str,
    regime: RegimeType,
    soft_regime: Optional[Dict[str, Any]],
    strategy_affinity: dict[str, dict[RegimeType, float]],
    soft_regime_enabled: bool,
    *,
    _parsed_cache: Optional[SoftRegimeResult] = None,
) -> float:
    """Compute the effective regime affinity for a strategy.

    In soft regime mode, returns the probability-weighted average affinity.
    In hard mode, returns the affinity for the detected regime type.
    """
    affinity_map = strategy_affinity.get(strategy, {})
    if soft_regime_enabled and soft_regime:
        try:
            parsed = _parsed_cache or SoftRegimeResult.from_dict(soft_regime)
            return sum(
                parsed.probability(item) * affinity_map.get(item, 0.5)
                for item in RegimeType
            )
        except Exception:
            logger.debug(
                "Failed to parse soft regime for affinity gate: %s",
                strategy,
                exc_info=True,
            )
    return affinity_map.get(regime, 0.5)


def any_strategy_eligible(
    symbol: str,
    timeframe: str,
    scope: str,
    regime: RegimeType,
    soft_regime: Optional[Dict[str, Any]],
    active_sessions: List[str],
    *,
    min_affinity_skip: float,
    target_index: dict[tuple[str, str], list[str]],
    strategy_sessions: dict[str, tuple[str, ...]],
    strategy_timeframes: dict[str, tuple[str, ...]],
    strategy_scopes: dict[str, frozenset[str]],
    strategy_affinity: dict[str, dict[RegimeType, float]],
    soft_regime_enabled: bool,
) -> bool:
    """Quick check: is there ANY strategy that could pass the evaluation loop?

    If min_affinity_skip is disabled (0) or any strategy has sufficient
    affinity, returns True.  Otherwise returns False, allowing the caller
    to skip expensive downstream work (market structure, voting, etc.).
    """
    if min_affinity_skip <= 0.0:
        return True
    strategies = target_index.get((symbol, timeframe), [])
    if not strategies:
        return False
    for strategy in strategies:
        allowed_sessions = strategy_sessions.get(strategy, ())
        if allowed_sessions and not any(s in allowed_sessions for s in active_sessions):
            continue
        allowed_tfs = strategy_timeframes.get(strategy, ())
        if allowed_tfs and timeframe not in allowed_tfs:
            continue
        allowed_scopes = strategy_scopes.get(
            strategy, frozenset(("intrabar", "confirmed"))
        )
        if scope not in allowed_scopes:
            continue
        aff = effective_affinity(
            strategy,
            regime,
            soft_regime,
            strategy_affinity,
            soft_regime_enabled,
        )
        if aff >= min_affinity_skip:
            return True
    return False
