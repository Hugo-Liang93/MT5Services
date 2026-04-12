"""SignalRuntime 状态查询与诊断方法（纯函数模块）。

所有函数接收 runtime 引用作为显式参数（ADR-002 模式）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.signals.contracts.execution_plan import (
    build_strategy_capability_summary,
    normalize_capability_contract,
)

if TYPE_CHECKING:
    from .runtime import SignalRuntime


def _normalize_number(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _normalize_number(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_number(item) for item in value]
    return value


def _snapshot_capability_reconciliation(runtime: SignalRuntime) -> dict[str, Any]:
    try:
        result = runtime.strategy_capability_reconciliation()
    except Exception as exc:
        return {
            "module_count": 0,
            "runtime_count": 0,
            "module_only": [],
            "runtime_only": [],
            "drift_items": [],
            "drift_count": 0,
            "reconciled": False,
            "module_snapshot": [],
            "runtime_snapshot": [],
            "module_runtime_contract": [],
            "error": str(exc),
        }

    if not isinstance(result, dict):
        return {
            "module_count": 0,
            "runtime_count": 0,
            "module_only": [],
            "runtime_only": [],
            "drift_items": [],
            "drift_count": 0,
            "reconciled": False,
            "module_snapshot": [],
            "runtime_snapshot": [],
            "module_runtime_contract": [],
            "error": "invalid reconciliation payload",
        }

    return {
        "module_count": int(result.get("module_count", 0)),
        "runtime_count": int(result.get("runtime_count", 0)),
        "module_only": list(result.get("module_only", []) or []),
        "runtime_only": list(result.get("runtime_only", []) or []),
        "drift_items": list(result.get("drift_items", []) or []),
        "drift_count": int(
            result.get("drift_count", 0) or len(result.get("drift_items", []) or [])
        ),
        "reconciled": bool(result.get("reconciled", False)),
        "module_snapshot": list(result.get("module_snapshot", []) or []),
        "runtime_snapshot": list(result.get("runtime_snapshot", []) or []),
        "module_runtime_contract": list(result.get("module_runtime_contract", []) or []),
    }


def _build_strategy_capability_execution_plan(
    runtime: SignalRuntime,
    capability_contract: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    normalized_capabilities = normalize_capability_contract(capability_contract)
    capability_by_name: dict[str, dict[str, Any]] = {
        str(item.get("name")): item
        for item in normalized_capabilities
        if str(item.get("name") or "").strip()
    }

    configured_targets: list[dict[str, str]] = []
    for target in runtime._targets:
        configured_targets.append(
            {
                "symbol": str(target.symbol),
                "timeframe": str(target.timeframe).upper(),
                "strategy": str(target.strategy),
            }
        )

    scheduled_targets: list[dict[str, str]] = []
    scheduled_keys: set[tuple[str, str, str]] = set()
    for (symbol, timeframe), strategies in runtime._target_index.items():
        for strategy in strategies:
            row = {
                "symbol": str(symbol),
                "timeframe": str(timeframe).upper(),
                "strategy": str(strategy),
            }
            scheduled_targets.append(row)
            scheduled_keys.add((row["symbol"], row["timeframe"], row["strategy"]))

    strategy_timeframes_policy: dict[str, tuple[str, ...]] = {
        str(strategy): tuple(str(tf).upper() for tf in (timeframes or ()))
        for strategy, timeframes in (runtime.policy.strategy_timeframes or {}).items()
        if timeframes
    }
    summary = build_strategy_capability_summary(
        capability_contract=normalized_capabilities,
        configured_strategies=[
            item["strategy"] for item in configured_targets if item.get("strategy")
        ],
        scheduled_strategies=[
            item["strategy"] for item in scheduled_targets if item.get("strategy")
        ],
        strategy_timeframes_policy=strategy_timeframes_policy,
    )

    filtered_targets: list[dict[str, Any]] = []
    filtered_reason_counts: dict[str, int] = {}
    for row in configured_targets:
        key = (row["symbol"], row["timeframe"], row["strategy"])
        if key in scheduled_keys:
            continue
        strategy = row["strategy"]
        reason = "prefilter_excluded"
        capability = capability_by_name.get(strategy)
        if capability is None:
            reason = "capability_missing"
        else:
            deployment = runtime.policy.get_strategy_deployment(strategy)
            if deployment is not None and not deployment.allows_runtime_evaluation():
                reason = "deployment_candidate"
            allowed = strategy_timeframes_policy.get(strategy, ())
            if reason == "prefilter_excluded" and allowed and row["timeframe"] not in allowed:
                reason = "strategy_timeframes_filtered"
        filtered_reason_counts[reason] = filtered_reason_counts.get(reason, 0) + 1
        filtered_targets.append(
            {
                **row,
                "reason": reason,
            }
        )

    result = {
        "configured_target_count": len(configured_targets),
        "scheduled_target_count": len(scheduled_targets),
        "filtered_target_count": len(filtered_targets),
        "filtered_reason_counts": filtered_reason_counts,
        "filtered_target_samples": filtered_targets[:50],
        "filtered_target_sample_overflow": max(0, len(filtered_targets) - 50),
    }
    result.update(summary)
    return result


def describe_voting(runtime: SignalRuntime) -> list[dict[str, Any]]:
    """返回投票引擎描述，供 API 诊断端点使用。"""
    groups: list[dict[str, Any]] = []
    for group_config, engine in runtime._voting_group_engines:
        groups.append({
            "name": group_config.name,
            "strategies": sorted(group_config.strategies),
            "engine": engine.describe(),
        })
    if runtime._voting_engine is not None:
        groups.append({
            "name": "consensus",
            "strategies": [],
            "engine": runtime._voting_engine.describe(),
        })
    return groups


def voting_groups_summary(runtime: SignalRuntime) -> list[dict[str, Any]]:
    """返回当前配置的 voting group 摘要。"""
    result: list[dict[str, Any]] = []
    for group_config, _engine in runtime._voting_group_engines:
        result.append(
            {
                "name": group_config.name,
                "strategies": sorted(group_config.strategies),
            }
        )
    return result


def get_regime_stability(
    runtime: SignalRuntime, symbol: str, timeframe: str
) -> dict[str, Any] | None:
    with runtime._regime_trackers_lock:
        tracker = runtime._regime_trackers.get((symbol, timeframe))
    return tracker.describe() if tracker else None


def get_regime_stability_map(runtime: SignalRuntime) -> dict[str, dict[str, Any]]:
    with runtime._regime_trackers_lock:
        snapshot = list(runtime._regime_trackers.items())
    return {
        f"{sym}/{tf}": tracker.describe() for (sym, tf), tracker in snapshot
    }


def get_voting_info(runtime: SignalRuntime) -> dict[str, Any]:
    voting_engine = runtime._voting_engine
    return {
        "voting_enabled": voting_engine is not None,
        "voting_config": voting_engine.describe() if voting_engine else None,
    }


def count_active_states(runtime: SignalRuntime) -> dict:
    with runtime._state_lock:
        snapshot = list(runtime._state_by_target.values())
    return {
        "active_confirmed_states": sum(
            1 for s in snapshot if s.confirmed_state != "idle"
        ),
    }


def compute_filter_window_stats(runtime: SignalRuntime) -> dict[str, Any]:
    """计算过滤窗口统计（从 _filter_window deque 聚合）。"""
    import time as _time

    now = _time.monotonic()
    cutoff = now - runtime._filter_window_seconds
    while runtime._filter_window and runtime._filter_window[0][0] < cutoff:
        runtime._filter_window.popleft()

    by_scope: dict[str, dict[str, Any]] = {}
    for _, scope, cat in runtime._filter_window:
        if scope not in by_scope:
            by_scope[scope] = {"passed": 0, "blocked": 0, "blocks": {}}
        s = by_scope[scope]
        if cat == "_pass":
            s["passed"] += 1
        else:
            s["blocked"] += 1
            s["blocks"][cat] = s["blocks"].get(cat, 0) + 1

    elapsed = now - runtime._filter_started_at
    window_secs = min(elapsed, runtime._filter_window_seconds)

    return {
        "filter_window_seconds": runtime._filter_window_seconds,
        "filter_window_elapsed": round(window_secs, 0),
        "filter_window_by_scope": by_scope,
    }


def _compute_drop_rates(runtime: SignalRuntime) -> dict[str, float]:
    dropped_events = max(0, int(runtime._dropped_events))
    dropped_confirmed = max(0, int(runtime._dropped_confirmed))
    dropped_intrabar = max(0, int(runtime._dropped_intrabar))
    stale_intrabar = max(0, int(runtime._intrabar_stale_drops))
    processed = max(1, int(runtime._processed_events))
    total_arrived = max(1, int(runtime._processed_events + dropped_events))
    return {
        "drop_vs_arrived_pct": round(
            dropped_events / total_arrived
            * 100,
            2,
        ),
        "intrabar_stale_vs_processed_pct": round(
            stale_intrabar / processed * 100, 2
        ),
        "intrabar_queue_drop_vs_arrived_pct": round(
            dropped_intrabar / total_arrived * 100, 2
        ),
        "confirmed_drop_vs_arrived_pct": round(
            dropped_confirmed / total_arrived * 100, 2
        ),
    }


def _snapshot_voting_stats(runtime: SignalRuntime) -> dict[str, Any]:
    src = runtime._voting_stats
    if not isinstance(src, dict):
        return {}
    snapshot: dict[str, Any] = {
        "mode": src.get("mode"),
        "calls": int(src.get("calls", 0)),
        "input_decisions": int(src.get("input_decisions", 0)),
        "fused_decisions": int(src.get("fused_decisions", 0)),
        "last_path": src.get("last_path"),
        "consensus": _normalize_number(src.get("consensus", {})),
        "groups": _normalize_number(src.get("groups", {})),
    }
    return snapshot


def _snapshot_intrabar_indicator_slos(runtime: SignalRuntime) -> dict[str, Any]:
    source = runtime.snapshot_source
    if source is None:
        return {}
    try:
        stats = source.get_performance_stats()
    except Exception:
        return {}
    if not isinstance(stats, dict):
        return {}

    intrabar = stats.get("intrabar")
    if not isinstance(intrabar, dict):
        return {}
    drop_rates = stats.get("drop_rates")
    if not isinstance(drop_rates, dict):
        drop_rates = {}

    queue = intrabar.get("queue")
    if not isinstance(queue, dict):
        queue = {}

    queue_age_ms_p95 = intrabar.get("queue_age_ms_p95")
    processing_latency_ms_p95 = intrabar.get("processing_latency_ms_p95")
    queue_age_sample_count = intrabar.get("queue_age_sample_count")
    processing_latency_sample_count = intrabar.get("processing_latency_sample_count")
    queue_age_ms_latest = intrabar.get("queue_age_ms_latest")
    processing_latency_ms_latest = intrabar.get("processing_latency_ms_latest")

    return {
        "drop_rates": {
            "intrabar_queue_drop_vs_arrived_pct": float(
                drop_rates.get("intrabar_queue_drop_vs_arrived_pct", 0.0) or 0.0
            )
        },
        "queue": {
            "size": int(queue.get("current_size", 0) or 0),
            "capacity": int(queue.get("maxsize", 0) or 0),
            "dropped_total": int(queue.get("dropped_total", 0) or 0),
        },
        "slo_ms": {
            "queue_age_p95": float(queue_age_ms_p95 or 0.0),
            "processing_latency_p95": float(processing_latency_ms_p95 or 0.0),
        },
        "sample_counts": {
            "queue_age_sample_count": int(queue_age_sample_count or 0),
            "processing_latency_sample_count": int(processing_latency_sample_count or 0),
        },
        "latest": {
            "queue_age_ms": float(queue_age_ms_latest or 0.0),
            "processing_latency_ms": float(processing_latency_ms_latest or 0.0),
        },
    }


def build_runtime_status(runtime: SignalRuntime) -> dict:
    """构建完整的运行时状态快照。"""
    market_structure_enabled = False
    if runtime._market_structure_analyzer is not None:
        market_structure_enabled = bool(
            runtime._market_structure_analyzer.config.enabled
        )
    capability_contract = runtime.strategy_capability_contract()
    strategy_scopes = {
        entry.get("name"): sorted(entry.get("valid_scopes") or [])
        for entry in capability_contract
        if entry.get("name")
    }
    strategy_deployments = {
        name: deployment.to_dict()
        for name, deployment in runtime.policy.strategy_deployments.items()
    }
    scheduled_target_count = sum(len(strategies) for strategies in runtime._target_index.values())

    return {
        "running": bool(runtime._thread and runtime._thread.is_alive()),
        "target_count": len(runtime._targets),
        "scheduled_target_count": scheduled_target_count,
        "filtered_target_count": max(
            0, len(runtime._targets) - scheduled_target_count
        ),
        "trigger_mode": {
            "confirmed_snapshot": runtime.enable_confirmed_snapshot,
        },
        "strategy_sessions": {
            name: list(sessions)
            for name, sessions in runtime.policy.strategy_sessions.items()
        },
        "strategy_deployments": strategy_deployments,
        "market_structure_enabled": market_structure_enabled,
        "market_structure_cache_entries": (
            runtime._market_structure_analyzer.cache_entries
            if runtime._market_structure_analyzer is not None
            else 0
        ),
        "strategy_scopes": strategy_scopes,
        "strategy_capability_matrix": list(capability_contract),
        "strategy_capability_execution_plan": _build_strategy_capability_execution_plan(
            runtime,
            capability_contract,
        ),
        "strategy_capability_reconciliation": _snapshot_capability_reconciliation(runtime),
        "intrabar_runtime_slos": _snapshot_intrabar_indicator_slos(runtime),
        "run_count": runtime._run_count,
        "processed_events": runtime._processed_events,
        "dropped_events": runtime._dropped_events,
        "dropped_intrabar_stale": runtime._intrabar_stale_drops,
        "dropped_confirmed": runtime._dropped_confirmed,
        "dropped_intrabar": runtime._dropped_intrabar,
        "drop_rates": _compute_drop_rates(runtime),
        "voting_stats": _snapshot_voting_stats(runtime),
        "confirmed_backpressure_waits": runtime._confirmed_backpressure_waits,
        "confirmed_backpressure_failures": runtime._confirmed_backpressure_failures,
        "confirmed_queue_size": runtime._confirmed_events.qsize(),
        "confirmed_queue_capacity": runtime._confirmed_events.maxsize,
        "intrabar_queue_size": runtime._intrabar_events.qsize(),
        "intrabar_queue_capacity": runtime._intrabar_events.maxsize,
        "last_run_at": (
            runtime._last_run_at.isoformat() if runtime._last_run_at else None
        ),
        "last_error": runtime._last_error,
        "filter_by_scope": {
            s: {
                "passed": d["passed"],
                "blocked": d["blocked"],
                "blocks": dict(d["blocks"]),
            }
            for s, d in runtime._filter_by_scope.items()
        },
        **compute_filter_window_stats(runtime),
        "htf_stale_warnings": runtime._htf_stale_counter[0],
        "warmup_skipped": runtime._warmup_skipped,
        "warmup_ready": (
            runtime._warmup_ready_fn()
            if runtime._warmup_ready_fn is not None
            else True
        ),
        "warmup_realtime_symbols": len(runtime._first_realtime_bar_seen),
        **count_active_states(runtime),
        "voting_groups": voting_groups_summary(runtime),
        "regime_map": get_regime_stability_map(runtime),
        "per_tf_eval_stats": dict(runtime._per_tf_eval_stats),
        "filter_realtime_status": (
            runtime.filter_chain.filter_status() if runtime.filter_chain is not None else {}
        ),
        "intrabar_trade_coordinator": (
            runtime._intrabar_trade_coordinator.status()
            if runtime._intrabar_trade_coordinator is not None
            else None
        ),
    }
