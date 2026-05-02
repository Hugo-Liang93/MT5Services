from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Deque, Iterable, Mapping

_METRIC_KEYS = ("trades", "pnl", "pf", "expectancy", "max_dd")


@dataclass(frozen=True)
class EntryMetaOverlayReport:
    status: str
    shadow_check: dict[str, Any]
    threshold_results: list[dict[str, Any]]
    filter_diagnostics: list[dict[str, Any]]
    baseline_metrics: dict[str, float | int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "shadow_check": self.shadow_check,
            "threshold_report": {
                "baseline_metrics": dict(self.baseline_metrics),
                "threshold_results": list(self.threshold_results),
            },
            "filter_diagnostics": list(self.filter_diagnostics),
        }


def build_entry_meta_overlay_report(
    baseline: Mapping[str, Any],
    shadow: Mapping[str, Any] | None,
    filters: Iterable[Mapping[str, Any]],
    max_dd_worsen_ratio: float = 0.10,
    min_trades: int = 10,
) -> EntryMetaOverlayReport:
    baseline_metrics = _extract_metrics(baseline)
    filter_results = list(filters)
    threshold_results: list[dict[str, Any]] = []
    filter_diagnostics: list[dict[str, Any]] = []

    for filter_result in filter_results:
        filter_metrics = _extract_metrics(filter_result)
        threshold = _extract_threshold(filter_result)
        decision = _decide_threshold(
            baseline_metrics,
            filter_metrics,
            max_dd_worsen_ratio=max_dd_worsen_ratio,
            min_trades=min_trades,
        )
        threshold_results.append(
            {
                "threshold": threshold,
                "metrics": filter_metrics,
                "entry_meta_overlay": filter_result.get("entry_meta_overlay", {}),
                "decision": decision,
            }
        )
        filter_diagnostics.append(
            {
                "threshold": threshold,
                "blocked_trade_attribution": build_blocked_trade_attribution(
                    baseline,
                    filter_result,
                ),
            }
        )

    return EntryMetaOverlayReport(
        status=_aggregate_status(threshold_results),
        shadow_check=_build_shadow_check(baseline_metrics, shadow),
        threshold_results=threshold_results,
        filter_diagnostics=filter_diagnostics,
        baseline_metrics=baseline_metrics,
    )


def build_blocked_trade_attribution(
    baseline: Mapping[str, Any],
    filter_result: Mapping[str, Any],
) -> dict[str, Any]:
    trades = baseline.get("raw_trades") or baseline.get("trades") or []
    trade_index: dict[tuple[str, str, str], Deque[Mapping[str, Any]]] = {}
    for trade in trades:
        key = _trade_match_key(trade)
        if key is not None:
            trade_index.setdefault(key, deque()).append(trade)
    events = (
        filter_result.get("execution_summary", {}).get("blocked_entry_events", [])
        or []
    )
    matched_pnl = 0.0
    matched_wins = 0
    matched_losses = 0
    unmatched_events = 0
    attributed_events: list[dict[str, Any]] = []
    matched_by_exit_reason: dict[str, dict[str, float | int]] = {}

    for event in events:
        source = event.get("source")
        if source not in (None, "", "entry_meta_overlay"):
            continue
        trades_for_key = trade_index.get(_event_match_key(event))
        if not trades_for_key:
            unmatched_events += 1
            continue
        trade = trades_for_key.popleft()

        pnl = _to_float(trade.get("pnl"))
        matched_pnl += pnl
        if pnl > 0:
            matched_wins += 1
        else:
            matched_losses += 1
        exit_reason = _exit_reason(trade)
        bucket = matched_by_exit_reason.setdefault(
            exit_reason,
            {"matched_trades": 0, "matched_pnl": 0.0},
        )
        bucket["matched_trades"] = int(bucket["matched_trades"]) + 1
        bucket["matched_pnl"] = float(bucket["matched_pnl"]) + pnl
        attributed_events.append(
            {
                "bar_time": event.get("bar_time"),
                "strategy": event.get("strategy"),
                "direction": event.get("direction"),
                "take_entry_prob": event.get("take_entry_prob"),
                "block_entry_prob": event.get("block_entry_prob"),
                "threshold": event.get("threshold", _extract_threshold(filter_result)),
                "matched_trade": _summarize_trade(trade),
            }
        )

    return {
        "matched_trades": len(attributed_events),
        "unmatched_events": unmatched_events,
        "matched_pnl": matched_pnl,
        "matched_wins": matched_wins,
        "matched_losses": matched_losses,
        "matched_by_exit_reason": dict(sorted(matched_by_exit_reason.items())),
        "attributed_events": attributed_events,
    }


def _decide_threshold(
    baseline_metrics: Mapping[str, float | int],
    filter_metrics: Mapping[str, float | int],
    *,
    max_dd_worsen_ratio: float,
    min_trades: int,
) -> dict[str, Any]:
    deltas = {
        key: filter_metrics[key] - baseline_metrics[key]
        for key in ("pnl", "pf", "expectancy", "max_dd")
    }
    if filter_metrics["trades"] < min_trades:
        return {
            "status": "refit",
            "reason": "insufficient_trades",
            "deltas": deltas,
            "min_trades": min_trades,
        }
    max_allowed_dd = baseline_metrics["max_dd"] * (1.0 + max_dd_worsen_ratio)
    if filter_metrics["max_dd"] > max_allowed_dd:
        return {
            "status": "rejected",
            "reason": "max_dd_breach",
            "deltas": deltas,
            "max_allowed_dd": max_allowed_dd,
        }
    if deltas["pnl"] < 0 or deltas["expectancy"] < 0:
        return {
            "status": "rejected",
            "reason": "return_expectancy_degraded",
            "deltas": deltas,
        }
    return_metrics_not_worse = (
        deltas["pnl"] >= 0 and deltas["pf"] >= 0 and deltas["expectancy"] >= 0
    )
    has_incremental_gain = (
        deltas["pnl"] > 0
        or deltas["pf"] > 0
        or deltas["expectancy"] > 0
        or deltas["max_dd"] < 0
    )
    if return_metrics_not_worse and has_incremental_gain:
        return {
            "status": "accepted",
            "reason": "incremental_gain",
            "deltas": deltas,
        }
    return {
        "status": "refit",
        "reason": "no_clear_incremental_gain",
        "deltas": deltas,
    }


def _extract_metrics(result: Mapping[str, Any]) -> dict[str, float | int]:
    raw = result.get("metrics", result)
    metrics = {
        "trades": int(_first_metric(raw, "trades", "total_trades")),
        "pnl": _to_float(_first_metric(raw, "pnl", "total_pnl")),
        "pf": _to_float(_first_metric(raw, "pf", "profit_factor")),
        "expectancy": _to_float(_first_metric(raw, "expectancy")),
        "max_dd": _to_float(_first_metric(raw, "max_dd", "max_drawdown")),
    }
    return metrics


def _first_metric(raw: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in raw:
            return raw[name]
    raise KeyError(f"Missing metric: {names[0]}")


def _build_shadow_check(
    baseline_metrics: Mapping[str, float | int],
    shadow: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if shadow is None:
        return {"status": "missing"}
    shadow_metrics = _extract_metrics(shadow)
    changed_metrics = [
        {
            "metric": key,
            "baseline": baseline_metrics[key],
            "shadow": shadow_metrics[key],
        }
        for key in _METRIC_KEYS
        if shadow_metrics[key] != baseline_metrics[key]
    ]
    if changed_metrics:
        return {"status": "failed", "changed_metrics": changed_metrics}
    return {"status": "passed", "changed_metrics": []}


def _aggregate_status(threshold_results: list[dict[str, Any]]) -> str:
    statuses = [item["decision"]["status"] for item in threshold_results]
    if "accepted" in statuses:
        return "accepted"
    if "rejected" in statuses:
        return "rejected"
    return "refit"


def _extract_threshold(result: Mapping[str, Any]) -> float | None:
    if "entry_meta_threshold" in result:
        return _to_float(result["entry_meta_threshold"])
    if "threshold" in result:
        return _to_float(result["threshold"])
    overlay = result.get("entry_meta_overlay")
    if isinstance(overlay, Mapping) and "threshold" in overlay:
        return _to_float(overlay["threshold"])
    return None


def _event_match_key(event: Mapping[str, Any]) -> tuple[str, str, str] | None:
    bar_time = event.get("bar_time")
    if bar_time is None:
        return None
    return (
        _normalize_time(bar_time),
        _normalize_text(event.get("strategy", "")),
        _normalize_text(event.get("direction", "")),
    )


def _trade_match_key(trade: Mapping[str, Any]) -> tuple[str, str, str] | None:
    entry_time = trade.get("entry_time")
    if entry_time is None:
        return None
    return (
        _normalize_time(entry_time),
        _normalize_text(trade.get("strategy", "")),
        _normalize_text(trade.get("direction", "")),
    )


def _summarize_trade(trade: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "entry_time": trade.get("entry_time"),
        "exit_time": trade.get("exit_time"),
        "strategy": trade.get("strategy"),
        "direction": trade.get("direction"),
        "pnl": _to_float(trade.get("pnl")),
        "exit_reason": _exit_reason(trade),
    }


def _exit_reason(trade: Mapping[str, Any]) -> str:
    return str(trade.get("close_reason") or trade.get("exit_reason") or "unknown")


def _normalize_text(value: Any) -> str:
    return str(value).strip().lower()


def _normalize_time(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value)
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _to_float(value: Any) -> float:
    return float(value)
