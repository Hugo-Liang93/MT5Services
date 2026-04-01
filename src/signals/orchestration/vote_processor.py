"""Vote processing logic extracted from SignalRuntime.

This module contains pure functions for:
- Vote fusion (deduplication of intrabar + confirmed decisions)
- Vote processing (dispatching to voting engines)
- Vote signal emission (state transition + persist + publish)
"""

from __future__ import annotations

import dataclasses as _dc
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import uuid4

from src.utils.common import timeframe_seconds

from .policy import RuntimeSignalState

logger = logging.getLogger(__name__)


def fuse_vote_decisions(
    symbol: str,
    timeframe: str,
    bar_time: datetime,
    scope: str,
    decisions: List[Any],
    fusion_cache: dict[tuple[str, str, datetime], dict[str, tuple[str, Any]]],
) -> List[Any]:
    """Merge intrabar + confirmed decisions for the same strategy within the same bar.

    Returns the deduplicated list of decisions to be voted on.
    """
    cache_key = (symbol, timeframe, bar_time)
    bucket = fusion_cache.setdefault(cache_key, {})
    for decision in decisions:
        existing = bucket.get(decision.strategy)
        if existing is None:
            bucket[decision.strategy] = (scope, decision)
            continue
        existing_scope, existing_decision = existing
        if existing_scope == "intrabar" and scope == "confirmed":
            bucket[decision.strategy] = (scope, decision)
            continue
        if existing_scope == scope:
            bucket[decision.strategy] = (scope, decision)
            continue
        if getattr(decision, "confidence", 0.0) >= getattr(
            existing_decision, "confidence", 0.0
        ):
            bucket[decision.strategy] = (scope, decision)
    prune_vote_fusion_cache(fusion_cache, symbol, timeframe, bar_time)
    return [item[1] for item in bucket.values()]


def prune_vote_fusion_cache(
    fusion_cache: dict[tuple[str, str, datetime], dict[str, tuple[str, Any]]],
    symbol: str,
    timeframe: str,
    current_bar_time: datetime,
) -> None:
    """Remove stale entries from the vote fusion cache."""
    keep_after = current_bar_time - timedelta(
        seconds=max(timeframe_seconds(timeframe) * 2, 1)
    )
    stale_keys = [
        key
        for key in fusion_cache
        if key[0] == symbol and key[1] == timeframe and key[2] < keep_after
    ]
    for key in stale_keys:
        fusion_cache.pop(key, None)
    _MAX_FUSION_CACHE = 2000
    if len(fusion_cache) > _MAX_FUSION_CACHE:
        sorted_keys = sorted(fusion_cache.keys(), key=lambda k: k[2])
        to_remove = sorted_keys[: len(sorted_keys) - _MAX_FUSION_CACHE]
        for key in to_remove:
            fusion_cache.pop(key, None)


def process_and_emit_vote_signal(
    vote_result: Any,
    group_name: str,
    symbol: str,
    timeframe: str,
    scope: str,
    regime_stability: float,
    regime_metadata: Dict[str, Any],
    indicators: Dict[str, Dict[str, float]],
    event_time: datetime,
    bar_time: datetime,
    *,
    state_by_target: dict[tuple[str, str, str], RuntimeSignalState],
    get_shard_lock: Callable[[str, str], Any],
    transition_confirmed_fn: Callable[..., Optional[Dict[str, Any]]],
    transition_intrabar_fn: Callable[..., Optional[Dict[str, Any]]],
    persist_fn: Callable[..., Any],
    publish_fn: Callable[..., None],
) -> None:
    """Execute state transition, persist and publish for a single vote result signal.

    regime_stability is recorded in metadata for observability but no longer
    multiplied into confidence.  The vote engine's avg_confidence + consensus
    bonus already produces values on the same scale as individual strategy
    confidence; an extra stability multiplier inflated vote confidence well
    above single-strategy levels, making a shared min_confidence threshold
    meaningless (vote easily 0.95+ while strategies sit at 0.40).
    """
    vote_result = _dc.replace(
        vote_result,
        metadata={
            **vote_result.metadata,
            "regime_stability": round(regime_stability, 4),
        },
    )
    group_key = (symbol, timeframe, group_name)
    shard_lock = get_shard_lock(symbol, timeframe)
    with shard_lock:
        group_state = state_by_target.setdefault(group_key, RuntimeSignalState())
    transition_metadata = (
        transition_confirmed_fn(
            group_state, vote_result.direction, event_time, bar_time, regime_metadata
        )
        if scope == "confirmed"
        else transition_intrabar_fn(
            group_state,
            vote_result.direction,
            vote_result.confidence,
            event_time,
            bar_time,
            regime_metadata,
        )
    )
    if transition_metadata is not None:
        signal_id = ""
        if transition_metadata.get("state_changed", True):
            record = persist_fn(
                vote_result, indicators=indicators, metadata=transition_metadata
            )
            signal_id = record.signal_id if record is not None else uuid4().hex[:12]
        elif scope == "confirmed" and vote_result.direction in ("buy", "sell"):
            signal_id = uuid4().hex[:12]
        publish_fn(vote_result, signal_id, scope, indicators, transition_metadata)


def process_voting(
    snapshot_decisions: List[Any],
    symbol: str,
    timeframe: str,
    scope: str,
    regime: Any,
    regime_stability: float,
    regime_metadata: Dict[str, Any],
    indicators: Dict[str, Dict[str, float]],
    event_time: datetime,
    bar_time: datetime,
    *,
    voting_engine: Optional[Any],
    voting_group_engines: list[tuple[Any, Any]],
    fusion_cache: dict[tuple[str, str, datetime], dict[str, tuple[str, Any]]],
    state_by_target: dict[tuple[str, str, str], RuntimeSignalState],
    get_shard_lock: Callable[[str, str], Any],
    transition_confirmed_fn: Callable[..., Optional[Dict[str, Any]]],
    transition_intrabar_fn: Callable[..., Optional[Dict[str, Any]]],
    persist_fn: Callable[..., Any],
    publish_fn: Callable[..., None],
) -> None:
    """Cross-strategy voting: dispatch to named voting groups or global consensus.

    Multi-group mode (voting_groups non-empty):
        Each group votes only on its member strategies' decisions,
        producing a signal named after group.name.
        Global consensus is automatically disabled.

    Single consensus mode (backward compatible):
        All independent strategies participate in voting,
        producing a strategy="consensus" signal.
    """
    if not snapshot_decisions:
        return
    snapshot_decisions = fuse_vote_decisions(
        symbol,
        timeframe,
        bar_time,
        scope,
        snapshot_decisions,
        fusion_cache,
    )
    if not snapshot_decisions:
        return

    emit_kwargs = dict(
        state_by_target=state_by_target,
        get_shard_lock=get_shard_lock,
        transition_confirmed_fn=transition_confirmed_fn,
        transition_intrabar_fn=transition_intrabar_fn,
        persist_fn=persist_fn,
        publish_fn=publish_fn,
    )

    # Multi-group mode
    if voting_group_engines:
        for group_config, group_engine in voting_group_engines:
            group_decisions = [
                d for d in snapshot_decisions if d.strategy in group_config.strategies
            ]
            if not group_decisions:
                continue
            vote_result = group_engine.vote(
                group_decisions,
                regime=regime,
                scope=scope,
                exclude_composite=False,
            )
            if vote_result is None:
                continue
            process_and_emit_vote_signal(
                vote_result,
                group_config.name,
                symbol,
                timeframe,
                scope,
                regime_stability,
                regime_metadata,
                indicators,
                event_time,
                bar_time,
                **emit_kwargs,
            )
        return

    # Single consensus mode (backward compatible)
    if voting_engine is None:
        return
    consensus = voting_engine.vote(snapshot_decisions, regime=regime, scope=scope)
    if consensus is None:
        return
    process_and_emit_vote_signal(
        consensus,
        voting_engine.CONSENSUS_STRATEGY_NAME,
        symbol,
        timeframe,
        scope,
        regime_stability,
        regime_metadata,
        indicators,
        event_time,
        bar_time,
        **emit_kwargs,
    )
