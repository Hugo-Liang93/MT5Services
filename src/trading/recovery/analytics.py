from __future__ import annotations

from collections import Counter, deque
from datetime import datetime
from typing import Any, Mapping


class RecoveryDecisionAnalytics:
    """In-memory recovery decision analytics projection.

    This class is a read-only observability helper. It does not decide whether
    to trade, mutate recovery cycles, or call external services.
    """

    def __init__(self, *, recent_limit: int = 100) -> None:
        if recent_limit <= 0:
            raise ValueError("recent_limit must be > 0")
        self._recent_limit = int(recent_limit)
        self._action_counts: Counter[str] = Counter()
        self._reason_counts: Counter[str] = Counter()
        self._status_counts: Counter[str] = Counter()
        self._execution_status_counts: Counter[str] = Counter()
        self._direction_policy_reason_counts: Counter[str] = Counter()
        self._direction_counts: Counter[str] = Counter()
        self._cost_gate_reason_counts: Counter[str] = Counter()
        self._cost_gate_allowed_counts: Counter[str] = Counter()
        self._recent_decisions: deque[dict[str, Any]] = deque(maxlen=self._recent_limit)
        self._net_exit_count = 0
        self._net_exit_total = 0.0
        self._net_exit_last: float | None = None
        self._net_exit_min: float | None = None
        self._net_exit_max: float | None = None
        self._entry_calibration_stats = {
            "spread_points": _NumericStats(),
            "required_points": _NumericStats(),
            "expected_net_points": _NumericStats(),
            "net_margin_points": _NumericStats(),
            "target_shortfall_points": _NumericStats(),
            "abs_price_change_points": _NumericStats(),
            "abs_pressure_delta": _NumericStats(),
            "buy_pressure": _NumericStats(),
            "sell_pressure": _NumericStats(),
        }
        self._entry_calibration_thresholds: dict[str, float] = {}

    def record(
        self,
        *,
        observed_at: datetime,
        action: str,
        reason: str,
        status: str,
        decision: Any | None = None,
        execution: Mapping[str, Any] | None = None,
    ) -> None:
        action_key = _clean(action)
        reason_key = _clean(reason)
        status_key = _clean(status)
        if action_key:
            self._action_counts[action_key] += 1
        if reason_key:
            self._reason_counts[reason_key] += 1
        if status_key:
            self._status_counts[status_key] += 1

        execution_payload = dict(execution or {})
        execution_status = _extract_execution_status(execution_payload)
        if execution_status:
            self._execution_status_counts[execution_status] += 1

        direction_policy = _extract_policy_payload(
            execution_payload, "direction_policy"
        )
        direction_policy_reason = _clean(direction_policy.get("reason"))
        if direction_policy_reason:
            self._direction_policy_reason_counts[direction_policy_reason] += 1
        direction = _clean(direction_policy.get("direction"))
        if direction:
            self._direction_counts[direction] += 1

        cost_gate = _extract_policy_payload(execution_payload, "cost_gate")
        cost_gate_reason = _clean(cost_gate.get("reason"))
        if cost_gate_reason:
            self._cost_gate_reason_counts[cost_gate_reason] += 1
        cost_gate_allowed = _allowed_key(cost_gate.get("allowed"))
        if cost_gate_allowed:
            self._cost_gate_allowed_counts[cost_gate_allowed] += 1

        self._record_entry_calibration(direction_policy, cost_gate)

        decision_metadata = _decision_metadata(decision)
        net_profit_points = _optional_float(decision_metadata.get("net_profit_points"))
        target_net_points = _optional_float(decision_metadata.get("target_net_points"))
        if net_profit_points is not None:
            self._record_net_exit(net_profit_points)

        self._recent_decisions.append(
            {
                "observed_at": observed_at.isoformat(),
                "action": action_key,
                "reason": reason_key,
                "status": status_key,
                "execution_status": execution_status,
                "direction_policy_reason": direction_policy_reason,
                "direction": direction,
                "cost_gate_reason": cost_gate_reason,
                "cost_gate_allowed": cost_gate_allowed,
                "net_profit_points": net_profit_points,
                "target_net_points": target_net_points,
            }
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "total_decisions": sum(self._action_counts.values()),
            "action_counts": _counter_payload(self._action_counts),
            "reason_counts": _counter_payload(self._reason_counts),
            "status_counts": _counter_payload(self._status_counts),
            "execution_status_counts": _counter_payload(self._execution_status_counts),
            "direction_policy_reason_counts": _counter_payload(
                self._direction_policy_reason_counts
            ),
            "direction_counts": _counter_payload(self._direction_counts),
            "cost_gate_reason_counts": _counter_payload(self._cost_gate_reason_counts),
            "cost_gate_allowed_counts": _counter_payload(
                self._cost_gate_allowed_counts
            ),
            "net_exit": {
                "sample_count": self._net_exit_count,
                "last_net_profit_points": self._net_exit_last,
                "avg_net_profit_points": (
                    round(self._net_exit_total / self._net_exit_count, 10)
                    if self._net_exit_count
                    else None
                ),
                "min_net_profit_points": self._net_exit_min,
                "max_net_profit_points": self._net_exit_max,
            },
            "entry_calibration": {
                "thresholds": dict(self._entry_calibration_thresholds),
                **{
                    key: stats.snapshot()
                    for key, stats in self._entry_calibration_stats.items()
                },
            },
            "recent_decisions": list(self._recent_decisions),
            "recent_limit": self._recent_limit,
        }

    def _record_net_exit(self, value: float) -> None:
        self._net_exit_count += 1
        self._net_exit_total += value
        self._net_exit_last = value
        self._net_exit_min = (
            value if self._net_exit_min is None else min(self._net_exit_min, value)
        )
        self._net_exit_max = (
            value if self._net_exit_max is None else max(self._net_exit_max, value)
        )

    def _record_entry_calibration(
        self,
        direction_policy: Mapping[str, Any],
        cost_gate: Mapping[str, Any],
    ) -> None:
        direction_metadata = _payload_metadata(direction_policy)
        price_change_points = _optional_float(
            direction_metadata.get("price_change_points")
        )
        if price_change_points is not None:
            self._entry_calibration_stats["abs_price_change_points"].record(
                abs(price_change_points)
            )

        pressure_delta = _optional_float(direction_metadata.get("pressure_delta"))
        if pressure_delta is not None:
            self._entry_calibration_stats["abs_pressure_delta"].record(
                abs(pressure_delta)
            )

        for stat_key in ("buy_pressure", "sell_pressure"):
            value = _optional_float(direction_metadata.get(stat_key))
            if value is not None:
                self._entry_calibration_stats[stat_key].record(value)

        for threshold_key in (
            "min_directional_move_points",
            "max_directional_move_points",
            "min_pressure_delta",
        ):
            self._record_entry_threshold(direction_metadata, threshold_key)

        cost_metadata = _payload_metadata(cost_gate)
        for stat_key in (
            "spread_points",
            "required_points",
            "expected_net_points",
            "net_margin_points",
            "target_shortfall_points",
        ):
            value = _optional_float(cost_metadata.get(stat_key))
            if value is not None:
                self._entry_calibration_stats[stat_key].record(value)

        for threshold_key in (
            "max_entry_spread_points",
            "recovery_target_points",
            "slippage_budget_points",
            "commission_points",
            "min_net_profit_points",
        ):
            self._record_entry_threshold(cost_metadata, threshold_key)

    def _record_entry_threshold(
        self,
        metadata: Mapping[str, Any],
        key: str,
    ) -> None:
        value = _optional_float(metadata.get(key))
        if value is not None:
            self._entry_calibration_thresholds[key] = value


class _NumericStats:
    def __init__(self, *, recent_limit: int = 500) -> None:
        self._count = 0
        self._total = 0.0
        self._last: float | None = None
        self._min: float | None = None
        self._max: float | None = None
        self._recent_values: deque[float] = deque(maxlen=recent_limit)

    def record(self, value: float) -> None:
        rounded = round(float(value), 10)
        self._count += 1
        self._total += rounded
        self._last = rounded
        self._min = rounded if self._min is None else min(self._min, rounded)
        self._max = rounded if self._max is None else max(self._max, rounded)
        self._recent_values.append(rounded)

    def snapshot(self) -> dict[str, Any]:
        recent_values = sorted(self._recent_values)
        return {
            "sample_count": self._count,
            "recent_sample_count": len(recent_values),
            "last": self._last,
            "avg": (round(self._total / self._count, 10) if self._count else None),
            "min": self._min,
            "max": self._max,
            "p50_recent": _percentile(recent_values, 50),
            "p75_recent": _percentile(recent_values, 75),
            "p90_recent": _percentile(recent_values, 90),
        }


def _extract_execution_status(execution: Mapping[str, Any]) -> str | None:
    status = _clean(execution.get("status"))
    if status:
        return status
    nested = execution.get("result")
    if isinstance(nested, Mapping):
        return _clean(nested.get("status"))
    return None


def _extract_policy_payload(
    execution: Mapping[str, Any],
    key: str,
) -> dict[str, Any]:
    direct = execution.get(key)
    if isinstance(direct, Mapping):
        return dict(direct)
    payload = execution.get("payload")
    if isinstance(payload, Mapping):
        metadata = payload.get("metadata")
        if isinstance(metadata, Mapping):
            nested = metadata.get(key)
            if isinstance(nested, Mapping):
                return dict(nested)
    return {}


def _payload_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata")
    return dict(metadata) if isinstance(metadata, Mapping) else {}


def _decision_metadata(decision: Any | None) -> dict[str, Any]:
    if decision is None:
        return {}
    metadata = getattr(decision, "metadata", None)
    return dict(metadata or {}) if isinstance(metadata, Mapping) else {}


def _allowed_key(value: Any) -> str | None:
    if value is True:
        return "allowed"
    if value is False:
        return "blocked"
    return None


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return round(parsed, 10)


def _counter_payload(counter: Counter[str]) -> dict[str, int]:
    return {str(key): int(value) for key, value in counter.items()}


def _percentile(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    rank = (len(values) - 1) * (float(percentile) / 100.0)
    lower = int(rank)
    upper = min(lower + 1, len(values) - 1)
    if lower == upper:
        return values[lower]
    weight = rank - lower
    return round(values[lower] + (values[upper] - values[lower]) * weight, 10)


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
