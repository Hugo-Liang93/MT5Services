from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StateEdgeEvaluationDecision:
    status: str
    reason: str
    pnl_delta: float
    pf_delta: float
    expectancy_delta: float
    max_dd_delta: float
    trade_delta: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "pnl_delta": self.pnl_delta,
            "pf_delta": self.pf_delta,
            "expectancy_delta": self.expectancy_delta,
            "max_dd_delta": self.max_dd_delta,
            "trade_delta": self.trade_delta,
        }


@dataclass(frozen=True)
class ThresholdGridReport:
    status: str
    best_threshold: float | None
    best_decision: StateEdgeEvaluationDecision | None
    threshold_results: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "best_threshold": self.best_threshold,
            "best_decision": (
                self.best_decision.to_dict() if self.best_decision is not None else None
            ),
            "threshold_results": list(self.threshold_results),
        }


@dataclass(frozen=True)
class StateEdgeOverlayValidationReport:
    status: str
    shadow_check: dict[str, Any]
    threshold_report: ThresholdGridReport
    filter_diagnostics: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "shadow_check": dict(self.shadow_check),
            "threshold_report": self.threshold_report.to_dict(),
            "filter_diagnostics": list(self.filter_diagnostics),
        }


def evaluate_overlay_increment(
    baseline: dict[str, Any],
    overlay: dict[str, Any],
    *,
    max_dd_worsen_ratio: float = 0.10,
    min_trades: int = 10,
) -> StateEdgeEvaluationDecision:
    base_metrics = baseline.get("metrics", {})
    overlay_metrics = overlay.get("metrics", {})

    base_pnl = _float_metric(base_metrics, "pnl")
    overlay_pnl = _float_metric(overlay_metrics, "pnl")
    base_pf = _float_metric(base_metrics, "pf")
    overlay_pf = _float_metric(overlay_metrics, "pf")
    base_expectancy = _float_metric(base_metrics, "expectancy")
    overlay_expectancy = _float_metric(overlay_metrics, "expectancy")
    base_dd = _float_metric(base_metrics, "max_dd")
    overlay_dd = _float_metric(overlay_metrics, "max_dd")
    base_trades = _int_metric(base_metrics, "trades")
    overlay_trades = _int_metric(overlay_metrics, "trades")

    pnl_delta = round(overlay_pnl - base_pnl, 10)
    pf_delta = round(overlay_pf - base_pf, 10)
    expectancy_delta = round(overlay_expectancy - base_expectancy, 10)
    max_dd_delta = round(overlay_dd - base_dd, 10)
    trade_delta = overlay_trades - base_trades

    if overlay_trades < min_trades:
        return StateEdgeEvaluationDecision(
            status="refit",
            reason="insufficient_trades",
            pnl_delta=pnl_delta,
            pf_delta=pf_delta,
            expectancy_delta=expectancy_delta,
            max_dd_delta=max_dd_delta,
            trade_delta=trade_delta,
        )

    dd_limit = base_dd * (1.0 + max_dd_worsen_ratio)
    if base_dd > 0.0 and overlay_dd > dd_limit:
        return StateEdgeEvaluationDecision(
            status="rejected",
            reason="max_dd_breach",
            pnl_delta=pnl_delta,
            pf_delta=pf_delta,
            expectancy_delta=expectancy_delta,
            max_dd_delta=max_dd_delta,
            trade_delta=trade_delta,
        )

    if pnl_delta < 0.0 or expectancy_delta < 0.0:
        return StateEdgeEvaluationDecision(
            status="rejected",
            reason="return_expectancy_degraded",
            pnl_delta=pnl_delta,
            pf_delta=pf_delta,
            expectancy_delta=expectancy_delta,
            max_dd_delta=max_dd_delta,
            trade_delta=trade_delta,
        )

    if (
        pnl_delta >= 0.0
        and pf_delta >= 0.0
        and expectancy_delta >= 0.0
        and (
            pnl_delta > 0.0
            or pf_delta > 0.0
            or expectancy_delta > 0.0
            or max_dd_delta < 0.0
        )
    ):
        return StateEdgeEvaluationDecision(
            status="accepted",
            reason="incremental_gain",
            pnl_delta=pnl_delta,
            pf_delta=pf_delta,
            expectancy_delta=expectancy_delta,
            max_dd_delta=max_dd_delta,
            trade_delta=trade_delta,
        )

    if pf_delta < 0.0 and expectancy_delta < 0.0:
        return StateEdgeEvaluationDecision(
            status="rejected",
            reason="pf_expectancy_degraded",
            pnl_delta=pnl_delta,
            pf_delta=pf_delta,
            expectancy_delta=expectancy_delta,
            max_dd_delta=max_dd_delta,
            trade_delta=trade_delta,
        )

    return StateEdgeEvaluationDecision(
        status="refit",
        reason="no_clear_incremental_gain",
        pnl_delta=pnl_delta,
        pf_delta=pf_delta,
        expectancy_delta=expectancy_delta,
        max_dd_delta=max_dd_delta,
        trade_delta=trade_delta,
    )


def build_threshold_grid_report(
    *,
    baseline: dict[str, Any],
    overlays: list[dict[str, Any]],
    max_dd_worsen_ratio: float = 0.10,
    min_trades: int = 10,
) -> ThresholdGridReport:
    threshold_results: list[dict[str, Any]] = []
    accepted: list[tuple[dict[str, Any], StateEdgeEvaluationDecision]] = []
    for overlay in overlays:
        decision = evaluate_overlay_increment(
            baseline,
            overlay,
            max_dd_worsen_ratio=max_dd_worsen_ratio,
            min_trades=min_trades,
        )
        item = {
            "threshold": _overlay_threshold(overlay),
            "metrics": dict(overlay.get("metrics", {})),
            "state_edge_overlay": dict(overlay.get("state_edge_overlay", {})),
            "decision": decision.to_dict(),
        }
        threshold_results.append(item)
        if decision.status == "accepted":
            accepted.append((overlay, decision))

    if not accepted:
        statuses = {item["decision"]["status"] for item in threshold_results}
        status = "rejected" if statuses == {"rejected"} else "refit"
        return ThresholdGridReport(
            status=status,
            best_threshold=None,
            best_decision=None,
            threshold_results=threshold_results,
        )

    best_overlay, best_decision = sorted(
        accepted,
        key=lambda pair: (
            pair[1].pnl_delta,
            pair[1].expectancy_delta,
            pair[1].pf_delta,
            -pair[1].max_dd_delta,
        ),
        reverse=True,
    )[0]
    return ThresholdGridReport(
        status="accepted",
        best_threshold=float(_overlay_threshold(best_overlay)),
        best_decision=best_decision,
        threshold_results=threshold_results,
    )


def build_overlay_validation_report(
    *,
    baseline: dict[str, Any],
    shadow: dict[str, Any] | None,
    filters: list[dict[str, Any]],
    max_dd_worsen_ratio: float = 0.10,
    min_trades: int = 10,
) -> StateEdgeOverlayValidationReport:
    threshold_report = build_threshold_grid_report(
        baseline=baseline,
        overlays=filters,
        max_dd_worsen_ratio=max_dd_worsen_ratio,
        min_trades=min_trades,
    )
    shadow_check = _build_shadow_check(baseline, shadow)
    status = threshold_report.status
    if shadow_check["status"] != "passed":
        status = "refit"
    return StateEdgeOverlayValidationReport(
        status=status,
        shadow_check=shadow_check,
        threshold_report=threshold_report,
        filter_diagnostics=[
            _build_filter_diagnostics(baseline=baseline, overlay=overlay)
            for overlay in filters
        ],
    )


def _build_shadow_check(
    baseline: dict[str, Any],
    shadow: dict[str, Any] | None,
) -> dict[str, Any]:
    if shadow is None:
        return {"status": "missing", "metric_deltas": {}}
    metric_deltas = _metric_deltas(
        baseline.get("metrics", {}),
        shadow.get("metrics", {}),
        keys=("trades", "pnl", "pf", "expectancy", "max_dd"),
    )
    changed = {
        key: value for key, value in metric_deltas.items() if abs(float(value)) > 1e-9
    }
    return {
        "status": "passed" if not changed else "failed",
        "metric_deltas": metric_deltas,
        "changed_metrics": changed,
    }


def _build_filter_diagnostics(
    *,
    baseline: dict[str, Any],
    overlay: dict[str, Any],
) -> dict[str, Any]:
    state_edge_overlay = dict(overlay.get("state_edge_overlay", {}) or {})
    execution_summary = dict(overlay.get("execution_summary", {}) or {})
    blocked_entry_events = [
        dict(event)
        for event in (execution_summary.get("blocked_entry_events", []) or [])
        if isinstance(event, dict)
    ]
    threshold = _overlay_threshold(overlay)
    return {
        "threshold": threshold,
        "diagnostic_scope": (
            "exact_blocked_entries" if blocked_entry_events else "aggregate_delta"
        ),
        "exact_blocked_trades_available": bool(blocked_entry_events),
        "blocked_entries": int(
            state_edge_overlay.get(
                "blocked",
                execution_summary.get("rejected_entries", 0),
            )
            or 0
        ),
        "observed_entries": int(state_edge_overlay.get("observed", 0) or 0),
        "allowed_entries": int(state_edge_overlay.get("allowed", 0) or 0),
        "missing_predictions": int(
            state_edge_overlay.get("missing_predictions", 0) or 0
        ),
        "filter_directions": list(
            state_edge_overlay.get("filter_directions", []) or []
        ),
        "blocked_by_direction": dict(
            state_edge_overlay.get("blocked_by_direction", {}) or {}
        ),
        "blocked_by_reason": dict(
            state_edge_overlay.get("blocked_by_reason", {}) or {}
        ),
        "execution_rejection_reasons": dict(
            execution_summary.get("rejection_reasons", {}) or {}
        ),
        "blocked_entry_summary": _blocked_entry_summary(blocked_entry_events),
        "blocked_entry_events": blocked_entry_events,
        "blocked_trade_attribution": _blocked_trade_attribution(
            baseline.get("raw_trades", []),
            blocked_entry_events,
        ),
        "strategy_deltas": _group_deltas(
            baseline.get("strategy_stats", {}),
            overlay.get("strategy_stats", {}),
        ),
        "regime_deltas": _group_deltas(
            baseline.get("regime_stats", {}),
            overlay.get("regime_stats", {}),
        ),
        "note": (
            "blocked_entry_events provide exact State Edge filter attribution when "
            "present; strategy/regime deltas still compare final trade distributions"
        ),
    }


def _blocked_entry_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(events),
        "by_strategy": _event_field_counts(events, "strategy"),
        "by_regime": _event_field_counts(events, "regime"),
        "by_direction": _event_field_counts(events, "direction"),
        "by_reason": _event_field_counts(events, "reason"),
    }


def _event_field_counts(
    events: list[dict[str, Any]],
    field: str,
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for event in events:
        value = str(event.get(field, "") or "").strip()
        if value:
            counts[value] += 1
    return dict(sorted(counts.items()))


def _blocked_trade_attribution(
    baseline_trades: Any,
    blocked_events: list[dict[str, Any]],
) -> dict[str, Any]:
    trades = [dict(item) for item in (baseline_trades or []) if isinstance(item, dict)]
    trade_index: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for trade in trades:
        key = _trade_match_key(
            trade.get("entry_time"),
            trade.get("strategy"),
            trade.get("direction"),
        )
        trade_index.setdefault(key, []).append(trade)

    attributed_events: list[dict[str, Any]] = []
    matched_trades: list[dict[str, Any]] = []
    unmatched_events: list[dict[str, Any]] = []
    for event in blocked_events:
        key = _trade_match_key(
            event.get("bar_time"),
            event.get("strategy"),
            event.get("direction"),
        )
        matches = trade_index.get(key, [])
        attributed = _attributed_event_payload(event)
        if matches:
            trade = matches[0]
            matched_trades.append(trade)
            attributed["matched_baseline_trade"] = True
            attributed["baseline_trade"] = _baseline_trade_payload(trade)
        else:
            unmatched_events.append(event)
            attributed["matched_baseline_trade"] = False
            attributed["baseline_trade"] = None
        attributed_events.append(attributed)

    return {
        "matching_key": ["bar_time=entry_time", "strategy", "direction"],
        "baseline_trades_available": bool(trades),
        "matched_trades": len(matched_trades),
        "unmatched_events": len(unmatched_events),
        "matched_pnl": round(
            sum(_float_trade_value(t, "pnl") for t in matched_trades), 10
        ),
        "matched_wins": sum(
            1 for trade in matched_trades if _float_trade_value(trade, "pnl") > 0.0
        ),
        "matched_losses": sum(
            1 for trade in matched_trades if _float_trade_value(trade, "pnl") <= 0.0
        ),
        "matched_by_exit_reason": _trade_field_counts(matched_trades, "exit_reason"),
        "matched_by_strategy": _matched_strategy_summary(matched_trades),
        "unmatched_by_strategy": _event_field_counts(unmatched_events, "strategy"),
        "attributed_events": attributed_events,
    }


def _trade_match_key(
    time_value: Any,
    strategy: Any,
    direction: Any,
) -> tuple[str, str, str]:
    return (
        str(time_value or "").strip(),
        str(strategy or "").strip(),
        str(direction or "").strip().lower(),
    )


def _attributed_event_payload(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "bar_time": event.get("bar_time"),
        "strategy": event.get("strategy"),
        "direction": event.get("direction"),
        "direction_probability": event.get("direction_probability"),
        "threshold": event.get("threshold"),
    }


def _baseline_trade_payload(trade: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "signal_id",
        "entry_time",
        "exit_time",
        "entry_price",
        "exit_price",
        "pnl",
        "pnl_pct",
        "exit_reason",
        "bars_held",
        "regime",
        "confidence",
    )
    return {key: trade.get(key) for key in keys if key in trade}


def _float_trade_value(trade: dict[str, Any], key: str) -> float:
    return float(trade.get(key, 0.0) or 0.0)


def _trade_field_counts(
    trades: list[dict[str, Any]],
    field: str,
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for trade in trades:
        value = str(trade.get(field, "") or "").strip()
        if value:
            counts[value] += 1
    return dict(sorted(counts.items()))


def _matched_strategy_summary(
    trades: list[dict[str, Any]],
) -> dict[str, dict[str, float | int]]:
    summary: dict[str, dict[str, float | int]] = {}
    for trade in trades:
        strategy = str(trade.get("strategy", "") or "").strip()
        if not strategy:
            continue
        pnl = _float_trade_value(trade, "pnl")
        item = summary.setdefault(
            strategy,
            {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0},
        )
        item["n"] = int(item["n"]) + 1
        if pnl > 0.0:
            item["wins"] = int(item["wins"]) + 1
        else:
            item["losses"] = int(item["losses"]) + 1
        item["pnl"] = round(float(item["pnl"]) + pnl, 10)
    return dict(sorted(summary.items()))


def _overlay_threshold(overlay: dict[str, Any]) -> Any:
    threshold = overlay.get("threshold", overlay.get("state_edge_threshold"))
    if threshold is None:
        state_edge_overlay = dict(overlay.get("state_edge_overlay", {}) or {})
        threshold = state_edge_overlay.get("threshold")
    return threshold


def _metric_deltas(
    baseline_metrics: dict[str, Any],
    overlay_metrics: dict[str, Any],
    *,
    keys: tuple[str, ...],
) -> dict[str, float | int]:
    deltas: dict[str, float | int] = {}
    for key in keys:
        if key == "trades":
            deltas[key] = _int_metric(overlay_metrics, key) - _int_metric(
                baseline_metrics, key
            )
        else:
            deltas[key] = round(
                _float_metric(overlay_metrics, key)
                - _float_metric(baseline_metrics, key),
                10,
            )
    return deltas


def _group_deltas(
    baseline_group: dict[str, Any],
    overlay_group: dict[str, Any],
) -> dict[str, dict[str, float | int]]:
    keys = set(baseline_group) | set(overlay_group)
    deltas: dict[str, dict[str, float | int]] = {}
    for key in sorted(keys):
        baseline_item = dict(baseline_group.get(key, {}) or {})
        overlay_item = dict(overlay_group.get(key, {}) or {})
        deltas[key] = {
            "n_delta": int(overlay_item.get("n", 0) or 0)
            - int(baseline_item.get("n", 0) or 0),
            "w_delta": int(overlay_item.get("w", 0) or 0)
            - int(baseline_item.get("w", 0) or 0),
            "pnl_delta": round(
                float(overlay_item.get("pnl", 0.0) or 0.0)
                - float(baseline_item.get("pnl", 0.0) or 0.0),
                10,
            ),
        }
    return deltas


def _float_metric(metrics: dict[str, Any], key: str) -> float:
    value = metrics.get(key, 0.0)
    return float(value or 0.0)


def _int_metric(metrics: dict[str, Any], key: str) -> int:
    value = metrics.get(key, 0)
    return int(value or 0)
