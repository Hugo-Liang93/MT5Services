from __future__ import annotations

import dataclasses as _dc
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ..confidence import apply_intrabar_decay
from ..evaluation.regime import RegimeType, SoftRegimeResult
from ..models import SignalEvent
from .policy import RuntimeSignalState
from .vote_processor import process_voting as _do_process_voting

if TYPE_CHECKING:
    from .runtime import SignalRuntime

logger = logging.getLogger(__name__)


def evaluate_strategies(
    runtime: "SignalRuntime",
    symbol: str,
    timeframe: str,
    scope: str,
    indicators: dict[str, dict[str, float]],
    regime: RegimeType,
    regime_metadata: dict[str, Any],
    event_time: datetime,
    bar_time: datetime,
    active_sessions: list[str],
) -> list:
    snapshot_decisions: list = []
    min_affinity_skip = runtime.policy.min_affinity_skip
    structure_resolved = False
    strategies = runtime._target_index.get((symbol, timeframe), [])
    shard_lock = runtime._get_shard_lock(symbol, timeframe)

    soft_parsed: SoftRegimeResult | None = None
    if runtime._soft_regime_enabled and regime_metadata.get("_soft_regime"):
        try:
            soft_parsed = SoftRegimeResult.from_dict(regime_metadata["_soft_regime"])
        except Exception:
            logger.info(
                "Failed to parse soft regime for %s/%s, falling back to hard regime",
                symbol,
                timeframe,
                exc_info=True,
            )

    event_impact = _resolve_event_impact_forecast(
        runtime, symbol, scope, event_time
    )

    for strategy in strategies:
        allowed_sessions = runtime.policy.strategy_sessions.get(strategy, ())
        if allowed_sessions and not any(
            session_name in allowed_sessions for session_name in active_sessions
        ):
            continue

        allowed_timeframes = runtime.policy.strategy_timeframes.get(strategy, ())
        if allowed_timeframes and timeframe not in allowed_timeframes:
            continue

        allowed_scopes = runtime._strategy_scopes.get(
            strategy, frozenset(("intrabar", "confirmed"))
        )
        if scope not in allowed_scopes:
            continue

        required_indicators = runtime._strategy_requirements.get(strategy, ())
        if required_indicators:
            missing = [ind for ind in required_indicators if ind not in indicators]
            if missing:
                _record_indicator_miss(runtime, symbol, timeframe, strategy, missing)
                continue
            scoped_indicators = {ind: indicators[ind] for ind in required_indicators}
        else:
            scoped_indicators = indicators

        if min_affinity_skip > 0.0:
            affinity = runtime._effective_affinity(
                strategy,
                regime,
                regime_metadata.get("_soft_regime"),
                _parsed_cache=soft_parsed,
            )
            if affinity < min_affinity_skip:
                runtime._affinity_gates_skipped += 1
                tf_skip = runtime._per_tf_skips.setdefault(
                    timeframe, {"affinity": 0, "raw_conf": 0}
                )
                tf_skip["affinity"] += 1
                continue
            regime_metadata["_pre_computed_affinity"] = affinity
        else:
            regime_metadata.pop("_pre_computed_affinity", None)

        with shard_lock:
            state = runtime._state_by_target.setdefault(
                (symbol, timeframe, strategy), RuntimeSignalState()
            )
        if not runtime._is_new_snapshot(
            state,
            scope=scope,
            event_time=event_time,
            bar_time=bar_time,
            indicators=scoped_indicators,
        ):
            continue

        if not structure_resolved and runtime._market_structure_analyzer is not None:
            structure_resolved = True
            try:
                structure_context = resolve_market_structure_context(
                    runtime,
                    symbol=symbol,
                    timeframe=timeframe,
                    scope=scope,
                    event_time=event_time,
                    bar_time=bar_time,
                    latest_close=regime_metadata.get("close_price"),
                )
            except Exception:
                logger.warning(
                    "Failed to build market structure context for %s/%s",
                    symbol,
                    timeframe,
                    exc_info=True,
                )
                structure_context = {}
            if structure_context:
                regime_metadata["market_structure"] = structure_context

        if event_impact is not None:
            regime_metadata["_event_impact_forecast"] = event_impact

        htf_spec = runtime._strategy_htf_config.get(strategy)
        htf_payload: dict[str, dict[str, dict[str, Any]]] = (
            runtime._resolve_htf_indicators(symbol, timeframe, htf_spec)
            if runtime._htf_indicators_enabled and htf_spec
            else {}
        )

        decision = runtime.service.evaluate(
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            indicators=scoped_indicators,
            metadata=regime_metadata,
            persist=False,
            htf_indicators=htf_payload,
        )
        decision = apply_confidence_adjustments(
            runtime, decision, symbol, timeframe, strategy, scope, regime_metadata
        )
        snapshot_decisions.append(decision)

        tf_eval = runtime._per_tf_eval_stats.setdefault(
            timeframe,
            {"evaluated": 0, "hold": 0, "buy_sell": 0, "below_min_conf": 0},
        )
        tf_eval["evaluated"] += 1
        if decision.direction in ("buy", "sell"):
            tf_eval["buy_sell"] += 1
        else:
            tf_eval["hold"] += 1

        transition_and_publish(
            runtime,
            state,
            decision,
            scope,
            event_time,
            bar_time,
            regime_metadata,
            scoped_indicators,
            indicators,
        )

    return snapshot_decisions


def _record_indicator_miss(
    runtime: "SignalRuntime",
    symbol: str,
    timeframe: str,
    strategy: str,
    missing: list[str],
) -> None:
    miss_key = (symbol, timeframe, strategy)
    runtime._indicator_miss_counts[miss_key] = (
        runtime._indicator_miss_counts.get(miss_key, 0) + 1
    )
    if len(runtime._indicator_miss_counts) > 600:
        keep_top = 200
        sorted_keys = sorted(
            runtime._indicator_miss_counts,
            key=runtime._indicator_miss_counts.get,  # type: ignore[arg-type]
            reverse=True,
        )
        keep_set = set(sorted_keys[:keep_top])
        for key in list(runtime._indicator_miss_counts):
            if key not in keep_set:
                runtime._indicator_miss_counts.pop(key, None)
    if (
        runtime._indicator_miss_counts[miss_key] <= 1
        or runtime._indicator_miss_counts[miss_key] % 100 == 0
    ):
        logger.warning(
            "Strategy %s/%s/%s skipped: missing indicators %s (count=%d)",
            symbol,
            timeframe,
            strategy,
            missing,
            runtime._indicator_miss_counts[miss_key],
        )


def _resolve_event_impact_forecast(
    runtime: "SignalRuntime",
    symbol: str,
    scope: str,
    event_time: datetime,
) -> dict[str, Any] | None:
    impact_analyzer = getattr(runtime, "_market_impact_analyzer", None)
    if impact_analyzer is None or scope != "confirmed":
        return None
    cache = runtime._event_impact_cache
    if event_time >= cache.get("expires_at", event_time):
        try:
            eco_service = getattr(runtime, "_economic_calendar_service", None)
            if eco_service is not None:
                upcoming_events = eco_service.get_high_impact_events(hours=2, limit=1)
                if upcoming_events:
                    cache["data"] = impact_analyzer.get_impact_forecast(
                        upcoming_events[0].event_name, symbol=symbol
                    )
                else:
                    cache["data"] = None
            cache["expires_at"] = event_time + timedelta(minutes=5)
        except Exception:
            cache["data"] = None
            cache["expires_at"] = event_time + timedelta(minutes=5)
    return cache.get("data")


def apply_confidence_adjustments(
    runtime: "SignalRuntime",
    decision: Any,
    symbol: str,
    timeframe: str,
    strategy: str,
    scope: str,
    regime_metadata: dict[str, Any],
) -> Any:
    if decision.direction in ("buy", "sell"):
        htf_mul, htf_dir = runtime._compute_htf_alignment(
            symbol, timeframe, decision.direction, scope
        )
        if htf_mul is not None:
            decision = _dc.replace(
                decision,
                confidence=min(1.0, decision.confidence * htf_mul),
            )
            regime_metadata["htf_direction"] = htf_dir
            regime_metadata["htf_alignment"] = (
                "aligned" if htf_mul >= 1.0 else "conflict"
            )
            regime_metadata["htf_confidence_multiplier"] = htf_mul
    if scope == "intrabar":
        decay = runtime._strategy_intrabar_decay.get(
            strategy, runtime._intrabar_confidence_factor
        )
        decision = apply_intrabar_decay(decision, scope, decay)
    if decision.confidence > 0 and decision.direction in ("buy", "sell"):
        floor = (
            runtime._confidence_floor if hasattr(runtime, "_confidence_floor") else 0.10
        )
        if decision.confidence < floor:
            decision = _dc.replace(decision, confidence=floor)
    return decision


def publish_signal_event(
    runtime: "SignalRuntime",
    decision: Any,
    signal_id: str,
    scope: str,
    indicators: dict[str, dict[str, float]],
    transition_metadata: dict[str, Any],
) -> None:
    with runtime._signal_listeners_lock:
        listeners = list(runtime._signal_listeners)
    if not listeners:
        return
    signal_state = transition_metadata.get("signal_state", "")
    event = SignalEvent(
        symbol=decision.symbol,
        timeframe=decision.timeframe,
        strategy=decision.strategy,
        direction=decision.direction,
        confidence=decision.confidence,
        signal_state=signal_state,
        scope=scope,
        indicators=indicators,
        metadata=transition_metadata,
        generated_at=decision.timestamp,
        signal_id=signal_id,
        reason=decision.reason,
    )
    pipeline_bus = getattr(runtime, "_pipeline_event_bus", None)
    trace_id = transition_metadata.get("signal_trace_id")
    if pipeline_bus is not None and trace_id:
        # 扩展 payload 包含置信度管线中间值 + Regime 快照
        eval_payload: dict[str, Any] = {}
        meta = decision.metadata or {}
        for key in (
            "raw_confidence", "regime_affinity", "post_affinity_confidence",
            "session_performance_multiplier", "post_performance_confidence",
            "regime", "regime_source", "regime_probabilities",
            "htf_direction", "htf_alignment", "htf_confidence_multiplier",
        ):
            if key in meta:
                eval_payload[key] = meta[key]
        pipeline_bus.emit_signal_evaluated(
            trace_id=trace_id,
            symbol=decision.symbol,
            timeframe=decision.timeframe,
            scope=scope,
            strategy=decision.strategy,
            direction=decision.direction,
            confidence=decision.confidence,
            signal_state=signal_state,
            **eval_payload,
        )
    listeners_to_remove: list[Any] = []
    for listener in listeners:
        listener_id = id(listener)
        try:
            listener(event)
            runtime._listener_fail_counts.pop(listener_id, None)
        except Exception as exc:
            fail_count = runtime._listener_fail_counts.get(listener_id, 0) + 1
            runtime._listener_fail_counts[listener_id] = fail_count
            listener_name = getattr(listener, "__name__", repr(listener))
            error_msg = (
                f"Signal listener error [{listener_name}]: {exc} "
                f"(failures={fail_count})"
            )
            runtime._last_error = error_msg
            logger.error(error_msg, exc_info=True)
            if fail_count >= runtime._LISTENER_MAX_CONSECUTIVE_FAILURES:
                logger.error(
                    "LISTENER CIRCUIT BREAK: %s reached %d consecutive failures, "
                    "auto-deregistering to prevent cascading errors",
                    listener_name,
                    fail_count,
                )
                listeners_to_remove.append(listener)
    if listeners_to_remove:
        with runtime._signal_listeners_lock:
            for listener in listeners_to_remove:
                try:
                    runtime._signal_listeners.remove(listener)
                except ValueError:
                    pass
                runtime._listener_fail_counts.pop(id(listener), None)


def transition_and_publish(
    runtime: "SignalRuntime",
    state: RuntimeSignalState,
    decision: Any,
    scope: str,
    event_time: datetime,
    bar_time: datetime,
    regime_metadata: dict[str, Any],
    scoped_indicators: dict[str, dict[str, float]],
    full_indicators: dict[str, dict[str, float]],
) -> None:
    transition_metadata = (
        runtime._transition_confirmed(
            state, decision.direction, event_time, bar_time, regime_metadata
        )
        if scope == "confirmed"
        else runtime._transition_intrabar(
            state,
            decision.direction,
            decision.confidence,
            event_time,
            bar_time,
            regime_metadata,
        )
    )
    if transition_metadata is None:
        return

    strategy_obj = runtime.service.get_strategy(decision.strategy)
    if strategy_obj is not None:
        transition_metadata["strategy_category"] = getattr(
            strategy_obj, "category", ""
        )

    if decision.strategy in runtime._voting_group_members:
        return

    signal_id = ""
    is_actionable = decision.direction in ("buy", "sell")
    if transition_metadata.get("state_changed", True):
        record = runtime.service.persist_decision(
            decision, indicators=scoped_indicators, metadata=transition_metadata
        )
        signal_id = record.signal_id if record is not None else uuid4().hex[:12]
    elif is_actionable:
        signal_id = uuid4().hex[:12]

    publish_signal_event(
        runtime, decision, signal_id, scope, full_indicators, transition_metadata
    )


def market_structure_lookback_bars(
    runtime: "SignalRuntime", timeframe: str
) -> int | None:
    analyzer = runtime._market_structure_analyzer
    if analyzer is None:
        return None
    analyzer_config = getattr(analyzer, "config", None)
    default_lookback = int(getattr(analyzer_config, "lookback_bars", 400))
    if str(timeframe).strip().upper() in ("M1", "M5"):
        return max(2, int(getattr(analyzer_config, "m1_lookback_bars", 120) or 120))
    return default_lookback


def resolve_market_structure_context(
    runtime: "SignalRuntime",
    *,
    symbol: str,
    timeframe: str,
    scope: str,
    event_time: datetime,
    bar_time: datetime,
    latest_close: float | None,
) -> dict[str, Any]:
    analyzer = runtime._market_structure_analyzer
    if analyzer is None:
        return {}

    return analyzer.analyze_cached(
        symbol,
        timeframe,
        scope=scope,
        event_time=event_time,
        latest_close=latest_close,
        lookback_bars_override=market_structure_lookback_bars(runtime, timeframe),
    )


def get_vote_emit_kwargs(runtime: "SignalRuntime") -> dict[str, Any]:
    return dict(
        state_by_target=runtime._state_by_target,
        get_shard_lock=runtime._get_shard_lock,
        transition_confirmed_fn=runtime._transition_confirmed,
        transition_intrabar_fn=runtime._transition_intrabar,
        persist_fn=runtime.service.persist_decision,
        publish_fn=runtime._publish_signal_event,
    )


def process_voting(
    runtime: "SignalRuntime",
    snapshot_decisions: list,
    symbol: str,
    timeframe: str,
    scope: str,
    regime: RegimeType,
    regime_stability: float,
    regime_metadata: dict[str, Any],
    indicators: dict[str, dict[str, float]],
    event_time: datetime,
    bar_time: datetime,
) -> None:
    # 获取当前事件的 trace_id 用于 pipeline 事件关联
    trace_id = ""
    if hasattr(runtime, "_current_trace_id"):
        trace_id = getattr(runtime, "_current_trace_id", "") or ""
    pipeline_bus = getattr(runtime, "_pipeline_bus", None)
    _do_process_voting(
        snapshot_decisions,
        symbol,
        timeframe,
        scope,
        regime,
        regime_stability,
        regime_metadata,
        indicators,
        event_time,
        bar_time,
        voting_engine=runtime._voting_engine,
        voting_group_engines=runtime._voting_group_engines,
        fusion_cache=runtime._vote_fusion_cache,
        pipeline_bus=pipeline_bus,
        trace_id=trace_id,
        **get_vote_emit_kwargs(runtime),
    )
