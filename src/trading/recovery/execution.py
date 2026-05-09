from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.risk.service import PreTradeRiskBlockedError, resolve_risk_failure_key

from .guard import RecoveryPreTradeGuard
from .models import PositionScalingIntent, RecoveryCycleState, RecoveryPolicy


def extract_blocked_pretrade_snapshot(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    for key in ("pre_trade_risk", "precheck", "dispatch_precheck"):
        snapshot = result.get(key)
        if not isinstance(snapshot, dict):
            continue
        verdict = str(snapshot.get("verdict") or "").strip().lower()
        blocked = bool(snapshot.get("blocked")) or verdict == "block"
        executable = snapshot.get("executable")
        if blocked or executable is False:
            return dict(snapshot)
    return None


def pretrade_block_reason(
    snapshot: dict[str, Any] | None,
    *,
    fallback: str,
) -> str:
    if not snapshot:
        return fallback
    reason = str(snapshot.get("reason") or "").strip()
    if reason:
        return reason
    resolved = resolve_risk_failure_key(snapshot)
    if resolved:
        return resolved
    for item in list(snapshot.get("checks") or []):
        if not isinstance(item, dict):
            continue
        item_reason = str(item.get("reason") or item.get("message") or "").strip()
        if item_reason:
            return item_reason
    return fallback


@dataclass(frozen=True)
class RecoveryExecutionCanaryPolicy:
    enabled: bool = False
    dry_run: bool = True
    order_kind: str = "market"
    deviation: int = 20
    magic: int = 0
    comment_prefix: str = "recovery"
    protective_stop_points: float | None = None

    @classmethod
    def from_config(cls, config: Any) -> "RecoveryExecutionCanaryPolicy":
        if isinstance(config, dict):
            getter = config.get
        else:
            getter = lambda key, default=None: getattr(config, key, default)
        return cls(
            enabled=_parse_bool(getter("enabled", False), default=False),
            dry_run=_parse_bool(getter("dry_run", True), default=True),
            order_kind=str(getter("order_kind", "market") or "market"),
            deviation=int(getter("deviation", 20) or 20),
            magic=int(getter("magic", 0) or 0),
            comment_prefix=str(getter("comment_prefix", "recovery") or "recovery"),
            protective_stop_points=_parse_optional_float(
                getter("protective_stop_points", None)
            ),
        )


class RecoveryExecutionAdapter:
    """Consumes recovery scaling intents through a guarded execution boundary."""

    def __init__(
        self,
        *,
        trading_port: Any,
        guard: RecoveryPreTradeGuard | None = None,
    ) -> None:
        self._trading_port = trading_port
        self._guard = guard or RecoveryPreTradeGuard()

    def execute_scaling_intent(
        self,
        *,
        recovery_policy: RecoveryPolicy,
        cycle: RecoveryCycleState,
        intent: PositionScalingIntent,
        canary_policy: RecoveryExecutionCanaryPolicy,
    ) -> dict[str, Any]:
        guard_decision = self._guard.assess(recovery_policy, cycle, intent)
        guard_payload = {
            "allowed": guard_decision.allowed,
            "reason": guard_decision.reason,
            **dict(guard_decision.details),
        }
        if not guard_decision.allowed:
            return {
                "status": "skipped",
                "reason": guard_decision.reason,
                "category": "recovery_guard",
                "guard": guard_payload,
            }
        if not canary_policy.enabled:
            return {
                "status": "skipped",
                "reason": "recovery_canary_disabled",
                "category": "recovery_canary",
                "guard": guard_payload,
            }
        if _requires_protective_stop(canary_policy) and not _has_positive_float(
            canary_policy.protective_stop_points
        ):
            return {
                "status": "skipped",
                "reason": "recovery_protective_stop_required",
                "category": "recovery_canary",
                "guard": guard_payload,
            }

        payload = self.build_trade_payload(
            recovery_policy=recovery_policy,
            cycle=cycle,
            intent=intent,
            canary_policy=canary_policy,
            guard=guard_payload,
        )
        try:
            result = self._trading_port.dispatch_operation("trade", payload)
        except PreTradeRiskBlockedError as exc:
            assessment = dict(getattr(exc, "assessment", {}) or {})
            reason = pretrade_block_reason(assessment, fallback=str(exc))
            return {
                "status": "skipped",
                "reason": reason,
                "category": "pre_trade_risk",
                "guard": guard_payload,
                "payload": payload,
                "pre_trade_risk": assessment,
                "error_message": str(exc),
            }
        except Exception as exc:  # noqa: BLE001 - trading port failures are dispatch blocks.
            return {
                "status": "skipped",
                "reason": str(exc) or type(exc).__name__,
                "category": "trading_port_failure",
                "guard": guard_payload,
                "payload": payload,
                "error_message": str(exc),
            }
        blocked_snapshot = extract_blocked_pretrade_snapshot(result)
        if blocked_snapshot is not None:
            return {
                "status": "skipped",
                "reason": pretrade_block_reason(
                    blocked_snapshot,
                    fallback="pre_trade_risk_blocked",
                ),
                "category": "pre_trade_risk",
                "guard": guard_payload,
                "payload": payload,
                "result": result,
                "pre_trade_risk": blocked_snapshot,
            }
        status = "dry_run" if canary_policy.dry_run else "submitted"
        return {
            "status": status,
            "reason": "dispatched",
            "category": "recovery_execution",
            "guard": guard_payload,
            "payload": payload,
            "result": result,
        }

    @staticmethod
    def build_trade_payload(
        *,
        recovery_policy: RecoveryPolicy,
        cycle: RecoveryCycleState,
        intent: PositionScalingIntent,
        canary_policy: RecoveryExecutionCanaryPolicy,
        guard: dict[str, Any],
    ) -> dict[str, Any]:
        request_id = f"recovery:{intent.cycle_id}:step:{intent.step_index}"
        metadata = {
            "execution_scope": "recovery_scaling",
            "recovery_cycle_id": intent.cycle_id,
            "recovery_step_index": intent.step_index,
            "recovery_reason": intent.reason,
            "source_signal_id": intent.source_signal_id,
            "account_key": intent.account_key,
            "strategy": intent.strategy,
            "timeframe": intent.timeframe,
            "guard": dict(guard),
            **dict(intent.metadata),
        }
        comment = (
            f"{canary_policy.comment_prefix}:{intent.cycle_id}:s{intent.step_index}"
        )
        payload = {
            "symbol": intent.symbol,
            "volume": intent.volume,
            "side": intent.direction,
            "order_kind": canary_policy.order_kind,
            "price": intent.entry_price,
            "deviation": int(canary_policy.deviation),
            "comment": comment,
            "magic": int(canary_policy.magic),
            "dry_run": bool(canary_policy.dry_run),
            "request_id": request_id,
            "trace_id": intent.source_signal_id or cycle.cycle_id,
            "action_id": request_id,
            "metadata": metadata,
        }
        if _requires_protective_stop(canary_policy):
            payload["sl"] = _protective_stop_price(
                direction=intent.direction,
                entry_price=float(intent.entry_price or 0.0),
                stop_points=float(canary_policy.protective_stop_points or 0.0),
                point=float(recovery_policy.point),
            )
        return payload


def _parse_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "":
            return default
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
        raise ValueError(f"invalid recovery execution canary boolean: {value!r}")
    return bool(value)


def _parse_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    parsed = float(value)
    return parsed if parsed > 0 else None


def _has_positive_float(value: float | None) -> bool:
    return value is not None and float(value) > 0


def _requires_protective_stop(policy: RecoveryExecutionCanaryPolicy) -> bool:
    return str(policy.order_kind or "market").strip().lower() == "market"


def _protective_stop_price(
    *,
    direction: str,
    entry_price: float,
    stop_points: float,
    point: float,
) -> float:
    distance = stop_points * point
    normalized = str(direction or "").strip().lower()
    if normalized == "buy":
        return round(entry_price - distance, 8)
    if normalized == "sell":
        return round(entry_price + distance, 8)
    raise ValueError("direction must be buy or sell")
