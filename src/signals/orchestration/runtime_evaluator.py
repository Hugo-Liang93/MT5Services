from __future__ import annotations

import dataclasses as _dc
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from src.calendar.policy import SignalEconomicPolicy
from ..confidence import apply_intrabar_decay
from ..evaluation.regime import RegimeType, SoftRegimeResult
from ..metadata_keys import MetadataKey as MK
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
    structure_resolved = False
    strategies = runtime._target_index.get((symbol, timeframe.upper()), [])
    shard_lock = runtime._get_shard_lock(symbol, timeframe)

    soft_parsed: SoftRegimeResult | None = None
    if runtime.soft_regime_enabled and regime_metadata.get(MK.REGIME_SOFT):
        try:
            soft_parsed = SoftRegimeResult.from_dict(regime_metadata[MK.REGIME_SOFT])
        except Exception:
            logger.info(
                "Failed to parse soft regime for %s/%s, falling back to hard regime",
                symbol,
                timeframe,
                exc_info=True,
            )

    event_impact = _resolve_event_impact_forecast(runtime, symbol, scope, event_time)

    for strategy in strategies:
        allowed_sessions = runtime.policy.strategy_sessions.get(strategy, ())
        if allowed_sessions and not any(
            session_name in allowed_sessions for session_name in active_sessions
        ):
            continue

        capability = runtime.policy.get_strategy_capability(strategy)
        if capability is None:
            logger.warning("Strategy capability missing for %s; skip evaluation", strategy)
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
    if scope != "confirmed":
        return None
    cache = runtime._event_impact_cache
    if event_time >= cache.get("expires_at", event_time):
        try:
            eco_service, policy = _resolve_eco_context(runtime)
            impact_analyzer = runtime.market_impact_analyzer
            if eco_service is not None and impact_analyzer is not None and policy is not None:
                upcoming_events = _get_upcoming_high_impact(
                    eco_service,
                    policy.filter_window.importance_min,
                )
                if upcoming_events:
                    cache["data"] = impact_analyzer.get_impact_forecast(
                        upcoming_events[0].event_name, symbol=symbol
                    )
                else:
                    cache["data"] = None
            else:
                cache["data"] = None
            cache["expires_at"] = event_time + timedelta(minutes=5)
        except Exception:
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


_EVENT_DECAY_TTL_SECONDS: float = 60.0
_EVENT_DECAY_CACHE_MAX: int = 128
_SIGNAL_EVENT_STATUSES = (
    "scheduled",
    "imminent",
    "pending_release",
    "released",
)


def _compute_economic_event_decay(
    runtime: "SignalRuntime",
    symbol: str,
) -> float:
    """计算经济事件渐进降权因子（0.0~1.0, 1.0=无影响）。"""
    import time as _time

    # --- 缓存命中检查 ---
    cache = runtime._event_decay_cache
    entry = cache.get(symbol)
    now_mono = _time.monotonic()
    if entry is not None and now_mono < entry["expires_at"]:
        return entry["decay"]

    # --- 获取 symbol-aware provider ---
    provider, policy = _resolve_eco_context(runtime)
    if provider is None or policy is None or not policy.enabled:
        return 1.0

    try:
        decay = _query_symbol_event_decay(provider, symbol, policy)
    except Exception:
        decay = 1.0

    cache[symbol] = {"decay": decay, "expires_at": now_mono + _EVENT_DECAY_TTL_SECONDS}

    # 防止缓存无限增长：先清除已过期条目，仍超限则淘汰最旧条目
    if len(cache) > _EVENT_DECAY_CACHE_MAX:
        expired = [k for k, v in cache.items() if now_mono >= v["expires_at"]]
        for k in expired:
            del cache[k]
        # 过期清理后仍超限：按 expires_at 升序淘汰，直到回到上限
        if len(cache) > _EVENT_DECAY_CACHE_MAX:
            by_age = sorted(cache.items(), key=lambda kv: kv[1]["expires_at"])
            for k, _ in by_age[: len(cache) - _EVENT_DECAY_CACHE_MAX]:
                del cache[k]

    return decay


def _resolve_eco_context(
    runtime: "SignalRuntime",
) -> tuple[Any | None, SignalEconomicPolicy | None]:
    """从 filter_chain 解析 signal-domain 经济事件 provider 与 policy。

    SignalRuntime 只应依赖 signal-domain filter 上已构造好的 policy，
    避免再从其他配置源推导第二套窗口语义。
    """
    fc = runtime.filter_chain
    if fc is not None and fc.economic_filter is not None:
        eco_filter = fc.economic_filter
        if eco_filter.provider is not None and eco_filter.policy is not None:
            return eco_filter.provider, eco_filter.policy
    return None, None


def _query_symbol_event_decay(
    provider: Any,
    symbol: str,
    policy: SignalEconomicPolicy,
) -> float:
    """对单个 symbol 查询最近相关高影响事件并计算 decay 因子。"""
    from src.calendar.economic_calendar.trade_guard import infer_symbol_context

    now = datetime.now(timezone.utc)
    context = infer_symbol_context(symbol)

    events = provider.get_events(
        start_time=now - timedelta(minutes=policy.query_window.lookback_minutes),
        end_time=now + timedelta(minutes=policy.query_window.lookahead_minutes),
        limit=5,
        countries=context["countries"] or None,
        currencies=context["currencies"] or None,
        statuses=list(_SIGNAL_EVENT_STATUSES),
        importance_min=policy.query_window.importance_min,
    )
    if not events:
        return 1.0

    best_delta: float | None = None
    for evt in events:
        event_time = getattr(evt, "event_time", None)
        if event_time is None:
            continue
        delta = (event_time - now).total_seconds() / 60.0
        if best_delta is None or abs(delta) < abs(best_delta):
            best_delta = delta

    if best_delta is None:
        return 1.0

    return policy.decay_for_delta(best_delta)


def apply_confidence_adjustments(
    runtime: "SignalRuntime",
    decision: Any,
    symbol: str,
    timeframe: str,
    strategy: str,
    scope: str,
    regime_metadata: dict[str, Any],
) -> Any:
    economic_decay_applied = False
    if scope == "intrabar":
        decay = runtime._strategy_intrabar_decay.get(
            strategy, runtime._intrabar_confidence_factor
        )
        decision = apply_intrabar_decay(decision, scope, decay)

    # 经济事件渐进降权：距离高影响事件越近，置信度衰减越大
    if decision.confidence > 0 and decision.direction in ("buy", "sell"):
        event_decay = _compute_economic_event_decay(runtime, symbol)
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
            armed_metadata[MK.STRATEGY_CATEGORY] = getattr(
                strategy_obj, "category", ""
            )
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
        return

    strategy_obj = runtime.service.get_strategy(decision.strategy)
    if strategy_obj is not None:
        transition_metadata[MK.STRATEGY_CATEGORY] = getattr(
            strategy_obj, "category", ""
        )
    deployment = runtime.policy.get_strategy_deployment(decision.strategy)
    if deployment is not None:
        transition_metadata[MK.STRATEGY_DEPLOYMENT] = deployment.to_dict()

    if decision.strategy in runtime._voting_group_members:
        return

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


def get_vote_emit_kwargs(runtime: "SignalRuntime") -> dict[str, Any]:
    return dict(
        state_by_target=runtime._state_by_target,
        get_shard_lock=runtime._get_shard_lock,
        transition_confirmed_fn=runtime._transition_confirmed,
        persist_fn=runtime.service.persist_decision,
        publish_fn=runtime._publish_signal_event,
        emit_voting_completed_fn=runtime._emit_voting_completed,
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
    trace_id = str(regime_metadata.get(MK.SIGNAL_TRACE_ID) or "")
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
        voting_stats=runtime._voting_stats,
        trace_id=trace_id,
        **get_vote_emit_kwargs(runtime),
    )
