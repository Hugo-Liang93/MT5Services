"""SignalModule 诊断与分析方法（从 service.py 提取的纯函数模块）。

所有函数接收 module 引用作为显式参数（ADR-002 模式）。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional

from .contracts.execution_plan import normalize_capability_contract

if TYPE_CHECKING:
    from .service import SignalModule


def strategy_diagnostics(
    module: SignalModule,
    *,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    scope: str = "confirmed",
    limit: int = 2000,
    conflict_warn_threshold: float = 0.35,
    hold_warn_threshold: float = 0.75,
    confidence_warn_threshold: float = 0.45,
) -> dict[str, Any]:
    from .analytics.diagnostics import DiagnosticThresholds

    rows = module.recent_signals(
        symbol=symbol, timeframe=timeframe, scope=scope, limit=limit,
    )
    thresholds = DiagnosticThresholds(
        conflict_warn_threshold=conflict_warn_threshold,
        hold_warn_threshold=hold_warn_threshold,
        confidence_warn_threshold=confidence_warn_threshold,
    )
    report = module.diagnostics_engine.build_report(
        rows, symbol=symbol, timeframe=timeframe, scope=scope, thresholds=thresholds,
    )
    return attach_expectancy_profile(module, report, symbol=symbol)


def daily_quality_report(
    module: SignalModule,
    *,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    scope: str = "confirmed",
    limit: int = 5000,
    conflict_warn_threshold: float = 0.35,
    hold_warn_threshold: float = 0.75,
    confidence_warn_threshold: float = 0.45,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    from .analytics.diagnostics import DiagnosticThresholds

    rows = module.recent_signals(
        symbol=symbol, timeframe=timeframe, scope=scope, limit=limit,
    )
    thresholds = DiagnosticThresholds(
        conflict_warn_threshold=conflict_warn_threshold,
        hold_warn_threshold=hold_warn_threshold,
        confidence_warn_threshold=confidence_warn_threshold,
    )
    report = module.diagnostics_engine.build_daily_quality_report(
        rows,
        symbol=symbol, timeframe=timeframe, scope=scope,
        thresholds=thresholds, now=now,
    )
    return attach_expectancy_profile(module, report, symbol=symbol)


def diagnostics_aggregate_summary(
    module: SignalModule,
    *,
    hours: int = 24,
    scope: str = "confirmed",
) -> dict[str, Any]:
    rows = module.summary(hours=hours, scope=scope)
    direction_totals: dict[str, int] = {}
    strategy_totals: dict[str, int] = {}
    for row in rows:
        direction = str(row.get("direction") or "unknown")
        strategy = str(row.get("strategy") or "unknown")
        count = int(row.get("count") or 0)
        direction_totals[direction] = direction_totals.get(direction, 0) + count
        strategy_totals[strategy] = strategy_totals.get(strategy, 0) + count
    top_strategies = sorted(
        (
            {"strategy": s, "count": c}
            for s, c in strategy_totals.items()
        ),
        key=lambda item: item["count"],
        reverse=True,
    )[:10]
    return {
        "hours": hours,
        "scope": scope,
        "rows_analyzed": len(rows),
        "direction_totals": direction_totals,
        "strategy_totals": strategy_totals,
        "top_strategies": top_strategies,
        "source": "repository.summary",
    }


def regime_report(
    module: SignalModule,
    *,
    symbol: str,
    timeframe: str,
    runtime: Optional[Any] = None,
) -> dict[str, Any]:
    indicators = module.indicator_source.get_all_indicators(symbol, timeframe)
    detail = module.regime_detector.detect_with_detail(indicators)
    stability = None
    if runtime is not None:
        try:
            stability = runtime.get_regime_stability(symbol, timeframe)
        except AttributeError:
            stability = None
    return {**detail, "symbol": symbol, "timeframe": timeframe, "stability": stability}


def strategy_capability_reconciliation(
    module: SignalModule,
    *,
    runtime_policy: Optional[Any] = None,
) -> dict[str, Any]:
    """对齐 module 与 policy 的能力快照并返回可观测对账结果。"""
    module_rows = list(normalize_capability_contract(module.strategy_capability_contract()))
    module_by_name = {row["name"]: row for row in module_rows if row.get("name")}

    runtime_rows: list[dict[str, Any]] = []
    if runtime_policy is not None:
        try:
            raw_runtime_rows = runtime_policy.strategy_capability_contract()
        except AttributeError as exc:
            raise TypeError(
                "runtime_policy must expose strategy_capability_contract()"
            ) from exc
        runtime_rows = list(normalize_capability_contract(raw_runtime_rows))
    runtime_by_name = {row["name"]: row for row in runtime_rows if row.get("name")}

    module_only = sorted(set(module_by_name.keys()) - set(runtime_by_name.keys()))
    runtime_only = sorted(set(runtime_by_name.keys()) - set(module_by_name.keys()))

    key_fields = (
        "valid_scopes",
        "needed_indicators",
        "needs_intrabar",
        "needs_htf",
        "regime_affinity",
        "htf_requirements",
    )
    drift_items: list[dict[str, Any]] = []
    for name in sorted(set(module_by_name) & set(runtime_by_name)):
        base = module_by_name[name]
        peer = runtime_by_name[name]
        for field in key_fields:
            if base.get(field) != peer.get(field):
                drift_items.append(
                    {
                        "strategy": name,
                        "field": field,
                        "module": base.get(field),
                        "runtime": peer.get(field),
                    }
                )
    return {
        "reconciled": not module_only and not runtime_only and not drift_items,
        "module_count": len(module_rows),
        "runtime_count": len(runtime_rows),
        "module_only": module_only,
        "runtime_only": runtime_only,
        "drift_items": drift_items,
        "module_snapshot": module_rows,
        "runtime_snapshot": runtime_rows,
    }


def strategy_winrates(
    module: SignalModule,
    *,
    hours: int = 168,
    symbol: Optional[str] = None,
) -> list[dict[str, Any]]:
    repository = module.repository
    if repository is None:
        return []
    try:
        return repository.fetch_winrates(hours=hours, symbol=symbol)
    except AttributeError:
        return []


def strategy_expectancy(
    module: SignalModule,
    *,
    hours: int = 168,
    symbol: Optional[str] = None,
) -> list[dict[str, Any]]:
    repository = module.repository
    if repository is None:
        return []
    try:
        return repository.fetch_expectancy_stats(hours=hours, symbol=symbol)
    except AttributeError:
        return []


def attach_expectancy_profile(
    module: SignalModule,
    report: dict[str, Any],
    *,
    symbol: Optional[str],
) -> dict[str, Any]:
    expectancy_rows = strategy_expectancy(module, symbol=symbol)
    if not expectancy_rows:
        report.setdefault("performance_profile", [])
        return report

    expectancy_by_strategy: dict[str, list[dict[str, Any]]] = {}
    for row in expectancy_rows:
        expectancy_by_strategy.setdefault(
            str(row.get("strategy") or "unknown"), []
        ).append(row)

    strategy_breakdown = report.get("strategy_breakdown")
    if isinstance(strategy_breakdown, list):
        for item in strategy_breakdown:
            strategy_name = str(item.get("strategy") or "unknown")
            candidates = expectancy_by_strategy.get(strategy_name, [])
            if not candidates:
                item["expectancy"] = None
                item["payoff_ratio"] = None
                continue
            best = max(
                candidates,
                key=lambda row: abs(float(row.get("expectancy") or 0.0)),
            )
            item["expectancy"] = best.get("expectancy")
            item["payoff_ratio"] = best.get("payoff_ratio")

    negative_rows = [
        row for row in expectancy_rows if float(row.get("expectancy") or 0.0) < 0.0
    ]
    recommendations = report.setdefault("recommendations", [])
    if negative_rows:
        recommendations.append(
            "negative_expectancy_detected: cut or retune strategies "
            "with negative net expectancy before expanding auto-trade"
        )
    report["performance_profile"] = expectancy_rows
    return report


def session_performance(module: SignalModule) -> dict[str, Any]:
    if not module.has_performance_tracker():
        return {"enabled": False}
    tracker = module.performance_tracker
    if tracker is None:
        return {"enabled": False}
    return tracker.describe()


def session_performance_ranking(module: SignalModule) -> list[dict[str, Any]]:
    if not module.has_performance_tracker():
        return []
    tracker = module.performance_tracker
    if tracker is None:
        return []
    return tracker.strategy_ranking()


def recent_by_trace_id(
    module: SignalModule,
    *,
    trace_id: str,
    scope: str = "all",
    limit: int = 2000,
) -> list[dict[str, Any]]:
    rows = module.recent_signals(scope=scope, limit=limit)
    return [
        row
        for row in rows
        if str((row.get("metadata") or {}).get("signal_trace_id", "")).strip()
        == trace_id
    ]


def dispatch_operation(
    module: SignalModule,
    operation: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Any:
    payload = payload or {}
    handlers = {
        "evaluate": lambda: module.evaluate(
            symbol=payload["symbol"],
            timeframe=payload["timeframe"],
            strategy=payload["strategy"],
            indicators=payload.get("indicators"),
            metadata=payload.get("metadata"),
            persist=payload.get("persist", True),
        ).to_dict(),
        "strategies": module.list_strategies,
        "strategy_capability_matrix": lambda: module.strategy_capability_matrix(),
        "strategy_requirements": lambda: {
            item["name"]: item.get("needed_indicators", [])
            for item in module.strategy_capability_matrix()
        },
        "strategy_capability_reconciliation": lambda: strategy_capability_reconciliation(
            module,
            runtime_policy=payload.get("runtime_policy"),
        ),
        "required_indicator_groups": lambda: [
            list(group) for group in module.required_indicator_groups()
        ],
        "available_indicators": lambda: [
            item.get("name") for item in module.indicator_source.list_indicators()
        ],
        "recent": lambda: module.recent_signals(
            symbol=payload.get("symbol"),
            timeframe=payload.get("timeframe"),
            strategy=payload.get("strategy"),
            direction=payload.get("direction"),
            scope=payload.get("scope", "confirmed"),
            limit=payload.get("limit", 200),
        ),
        "summary": lambda: module.summary(
            hours=payload.get("hours", 24),
            scope=payload.get("scope", "confirmed"),
        ),
        "strategy_diagnostics": lambda: strategy_diagnostics(
            module,
            symbol=payload.get("symbol"),
            timeframe=payload.get("timeframe"),
            scope=payload.get("scope", "confirmed"),
            limit=payload.get("limit", 2000),
            conflict_warn_threshold=payload.get("conflict_warn_threshold", 0.35),
            hold_warn_threshold=payload.get("hold_warn_threshold", 0.75),
            confidence_warn_threshold=payload.get(
                "confidence_warn_threshold", 0.45
            ),
        ),
        "daily_quality_report": lambda: daily_quality_report(
            module,
            symbol=payload.get("symbol"),
            timeframe=payload.get("timeframe"),
            scope=payload.get("scope", "confirmed"),
            limit=payload.get("limit", 5000),
            conflict_warn_threshold=payload.get("conflict_warn_threshold", 0.35),
            hold_warn_threshold=payload.get("hold_warn_threshold", 0.75),
            confidence_warn_threshold=payload.get(
                "confidence_warn_threshold", 0.45
            ),
        ),
        "diagnostics_aggregate_summary": lambda: diagnostics_aggregate_summary(
            module,
            hours=payload.get("hours", 24),
            scope=payload.get("scope", "confirmed"),
        ),
        "regime_report": lambda: regime_report(
            module,
            symbol=payload["symbol"],
            timeframe=payload["timeframe"],
            runtime=payload.get("runtime"),
        ),
        "strategy_winrates": lambda: strategy_winrates(
            module,
            hours=payload.get("hours", 168),
            symbol=payload.get("symbol"),
        ),
        "strategy_expectancy": lambda: strategy_expectancy(
            module,
            hours=payload.get("hours", 168),
            symbol=payload.get("symbol"),
        ),
        "session_performance": lambda: session_performance(module),
        "session_performance_ranking": lambda: session_performance_ranking(module),
        "recent_by_trace_id": lambda: recent_by_trace_id(
            module,
            trace_id=payload["trace_id"],
            scope=payload.get("scope", "all"),
            limit=payload.get("limit", 2000),
        ),
    }
    if operation not in handlers:
        raise ValueError(f"unsupported signal operation: {operation}")
    return handlers[operation]()
