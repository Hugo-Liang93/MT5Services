from __future__ import annotations

import dataclasses as _dc
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ..confidence import apply_intrabar_decay
from ..evaluation.regime import RegimeType
from ..metadata_keys import MetadataKey as MK
from ..models import SignalEvent
from .policy import RuntimeSignalState

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
    structure_resolved = False
    strategies = runtime._target_index.get((symbol, timeframe.upper()), [])
    shard_lock = runtime._get_shard_lock(symbol, timeframe)

    event_impact = _resolve_event_impact_forecast(runtime, symbol, scope, event_time)

    for strategy in strategies:
        allowed_sessions = runtime.policy.strategy_sessions.get(strategy, ())
        if allowed_sessions and not any(
            session_name in allowed_sessions for session_name in active_sessions
        ):
            continue

        capability = runtime.policy.get_strategy_capability(strategy)
        if capability is None:
            logger.warning(
                "Strategy capability missing for %s; skip evaluation", strategy
            )
            continue
        if scope not in capability.valid_scopes:
            continue

        required_indicators = capability.needed_indicators
        if required_indicators:
            missing = [ind for ind in required_indicators if ind not in indicators]
            if missing:
                _record_indicator_miss(runtime, symbol, timeframe, strategy, missing)
                continue
            scoped_indicators = {ind: indicators[ind] for ind in required_indicators}
        else:
            scoped_indicators = indicators

        regime_metadata.pop(MK.PRE_COMPUTED_AFFINITY, None)

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
                    latest_close=regime_metadata.get(MK.CLOSE_PRICE),
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
                regime_metadata[MK.MARKET_STRUCTURE] = structure_context

        if event_impact is not None:
            regime_metadata[MK.EVENT_IMPACT_FORECAST] = event_impact

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
            runtime,
            decision,
            symbol,
            timeframe,
            strategy,
            scope,
            regime_metadata,
            event_time,
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
    if scope != "confirmed":
        return None
    cache = runtime._event_impact_cache
    if event_time < cache.get("expires_at", event_time):
        return cache.get("data")

    service = runtime.economic_decay_service
    impact_analyzer = runtime.market_impact_analyzer
    eco_service = runtime.economic_calendar_service
    if service is None or impact_analyzer is None or eco_service is None:
        cache["data"] = None
        cache["expires_at"] = event_time + timedelta(minutes=5)
        return None

    try:
        upcoming_events = _get_upcoming_high_impact(
            eco_service,
            service.policy.filter_window.importance_min,
        )
    except (ConnectionError, TimeoutError, OSError, RuntimeError) as exc:
        logger.warning(
            "event impact forecast runtime degrade for %s: %s: %s",
            symbol,
            type(exc).__name__,
            exc,
        )
        cache["data"] = None
        cache["expires_at"] = event_time + timedelta(minutes=5)
        return None

    if upcoming_events:
        cache["data"] = impact_analyzer.get_impact_forecast(
            upcoming_events[0].event_name, symbol=symbol
        )
    else:
        cache["data"] = None
    cache["expires_at"] = event_time + timedelta(minutes=5)
    return cache.get("data")


def _get_upcoming_high_impact(provider: Any, importance_min: int) -> list[Any]:
    """查询未来 2 小时内高影响事件。"""
    return provider.get_high_impact_events(
        hours=2,
        limit=1,
        importance_min=importance_min,
    )


def apply_confidence_adjustments(
    runtime: "SignalRuntime",
    decision: Any,
    symbol: str,
    timeframe: str,
    strategy: str,
    scope: str,
    regime_metadata: dict[str, Any],
    event_time: datetime,
) -> Any:
    economic_decay_applied = False
    if scope == "intrabar":
        decay = runtime._strategy_intrabar_decay.get(
            strategy, runtime._intrabar_confidence_factor
        )
        decision = apply_intrabar_decay(decision, scope, decay)

    # 经济事件渐进降权：距离高影响事件越近，置信度衰减越大
    if decision.confidence > 0 and decision.direction in ("buy", "sell"):
        service = runtime.economic_decay_service
        event_decay = (
            service.decay_factor(symbol, at_time=event_time)
            if service is not None
            else 1.0
        )
        if event_decay < 1.0:
            economic_decay_applied = True
            adjusted = decision.confidence * event_decay
            trace = list(decision.confidence_trace)
            trace.append(("economic_event_decay", round(adjusted, 4)))
            decision = _dc.replace(
                decision,
                confidence=adjusted,
                confidence_trace=trace,
            )

    if (
        decision.confidence > 0
        and decision.direction in ("buy", "sell")
        and not economic_decay_applied
    ):
        floor = runtime.confidence_floor
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
    signal_state = transition_metadata.get(MK.SIGNAL_STATE, "")
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
    pipeline_bus = runtime.get_pipeline_event_bus()
    trace_id = transition_metadata.get(MK.SIGNAL_TRACE_ID)
    if pipeline_bus is not None and trace_id:
        # 扩展 payload 包含置信度管线中间值 + Regime 快照
        eval_payload: dict[str, Any] = {}
        meta = decision.metadata or {}
        for key in (
            "raw_confidence",
            "regime_affinity",
            "post_affinity_confidence",
            "session_performance_multiplier",
            "post_performance_confidence",
            "regime",
            "regime_source",
            "regime_probabilities",
            "htf_direction",
            "htf_alignment",
            "htf_confidence_multiplier",
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
    transition_metadata = None
    if scope == "confirmed":
        transition_metadata = runtime._transition_confirmed(
            state, decision.direction, event_time, bar_time, regime_metadata
        )

    # ── Intrabar 交易协调：每次 intrabar 评估都更新 bar 计数 ──────────
    # coordinator 独立运行，在 intrabar 评估时递增 stability counter 并在
    # 达标时独立发布 intrabar_armed 信号，直接触发盘中交易。
    armed_state: str | None = None
    if (
        scope == "intrabar"
        and decision.direction in ("buy", "sell")
        and runtime._intrabar_trade_coordinator is not None
    ):
        armed_state = runtime._intrabar_trade_coordinator.update(
            symbol=decision.symbol,
            parent_tf=decision.timeframe,
            strategy=decision.strategy,
            direction=decision.direction,
            confidence=decision.confidence,
            parent_bar_time=bar_time,
        )

    # ── Coordinator armed 信号独立发布，不受状态机门控 ──────────────
    # coordinator 的 intrabar_armed 信号直接触发 TradeExecutor，
    # 不应因状态机无转换（transition_metadata=None）而被丢弃。
    if armed_state is not None:
        armed_metadata = dict(regime_metadata)
        armed_metadata[MK.SIGNAL_STATE] = armed_state
        armed_metadata[MK.STATE_CHANGED] = True
        armed_metadata[MK.INTRABAR_STABLE_BARS] = (
            runtime._intrabar_trade_coordinator.policy.get_min_stable_bars(
                decision.strategy
            )
        )
        armed_metadata[MK.INTRABAR_PARENT_BAR_TIME] = bar_time
        strategy_obj = runtime.service.get_strategy(decision.strategy)
        if strategy_obj is not None:
            armed_metadata[MK.STRATEGY_CATEGORY] = getattr(strategy_obj, "category", "")
        deployment = runtime.policy.get_strategy_deployment(decision.strategy)
        if deployment is not None:
            armed_metadata[MK.STRATEGY_DEPLOYMENT] = deployment.to_dict()
        armed_signal_id = uuid4().hex[:12]
        # 持久化 coordinator armed 决策
        runtime.service.persist_decision(
            decision, indicators=scoped_indicators, metadata=armed_metadata
        )
        publish_signal_event(
            runtime,
            decision,
            armed_signal_id,
            scope,
            full_indicators,
            armed_metadata,
        )

    if transition_metadata is None:
        pipeline_bus = runtime.get_pipeline_event_bus()
        trace_id = str(regime_metadata.get(MK.SIGNAL_TRACE_ID) or "").strip()
        if pipeline_bus is not None and trace_id:
            meta = decision.metadata or {}
            eval_payload: dict[str, Any] = {}
            for key in (
                "raw_confidence",
                "regime_affinity",
                "post_affinity_confidence",
                "session_performance_multiplier",
                "post_performance_confidence",
                "regime",
                "regime_source",
                "regime_probabilities",
                "htf_direction",
                "htf_alignment",
                "htf_confidence_multiplier",
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
                signal_state=(
                    "no_signal" if decision.direction not in ("buy", "sell") else ""
                ),
                **eval_payload,
            )
        return

    strategy_obj = runtime.service.get_strategy(decision.strategy)
    if strategy_obj is not None:
        transition_metadata[MK.STRATEGY_CATEGORY] = getattr(
            strategy_obj, "category", ""
        )
    deployment = runtime.policy.get_strategy_deployment(decision.strategy)
    if deployment is not None:
        transition_metadata[MK.STRATEGY_DEPLOYMENT] = deployment.to_dict()

    signal_id = ""
    is_actionable = decision.direction in ("buy", "sell")
    if transition_metadata.get(MK.STATE_CHANGED, True):
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
    analyzer_config = analyzer.config
    default_lookback = int(analyzer_config.lookback_bars)
    if str(timeframe).strip().upper() in ("M1", "M5"):
        return max(2, int(analyzer_config.m1_lookback_bars or 120))
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
