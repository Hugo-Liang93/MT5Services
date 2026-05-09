from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from src.backtesting.tick_replay import RecoveryCanaryGatePolicy
from src.clients.mt5_market import MT5MarketClient, Quote, Tick
from src.config import (
    get_economic_config,
    get_risk_config,
    get_runtime_identity,
    get_trading_config,
    load_db_settings,
    load_mt5_settings,
    reload_configs,
    set_current_environment,
    set_current_instance_name,
)
from src.ops.mt5_session_gate import ensure_mt5_session_gate_or_raise
from src.persistence.db import TimescaleWriter
from src.trading.application.module import TradingModule
from src.trading.recovery.canary import (
    close_dry_run_recovery_cycle,
    close_initial_recovery_cycle,
    close_recovery_canary_positions,
    evaluate_current_recovery_step,
    execute_recovery_canary,
    open_initial_recovery_cycle,
    verify_initial_recovery_cleanup,
)
from src.trading.recovery.execution import RecoveryExecutionCanaryPolicy
from src.trading.recovery.models import RecoveryPolicy
from src.trading.runtime.registry import TradingAccountRegistry
from src.trading.runtime.trade_frequency import TradeCommandAuditFrequencyProvider
from src.trading.state.store import TradingStateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recovery_canary",
        description="Run a demo-only bounded recovery dry-run canary from MT5 ticks.",
    )
    parser.add_argument("--environment", default="demo", choices=["demo", "live"])
    parser.add_argument("--instance", default="demo-main")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--direction", default="buy", choices=["buy", "sell"])
    parser.add_argument("--tick-limit", type=int, default=5000)
    parser.add_argument("--lookback-seconds", type=int, default=3600)
    parser.add_argument("--start", default=None, help="Optional ISO8601 UTC start time")
    parser.add_argument("--auto-launch-terminal", action="store_true")
    parser.add_argument(
        "--live-cycle",
        action="store_true",
        help="Open or dry-run an initial cycle from the current quote, then evaluate current tick expansion.",
    )
    parser.add_argument(
        "--submit-initial-order",
        action="store_true",
        help="With --live-cycle, submit the initial demo order; recovery expansion remains dry-run unless --submit-recovery-order is also set.",
    )
    parser.add_argument(
        "--submit-recovery-order",
        action="store_true",
        help="With --live-cycle and --submit-initial-order, submit one bounded demo recovery step and auto-clean all canary positions.",
    )
    parser.add_argument(
        "--keep-initial-open",
        action="store_true",
        help="With --submit-initial-order, leave the demo initial order open instead of auto-closing it.",
    )
    parser.add_argument(
        "--initial-cleanup-delay-seconds",
        type=float,
        default=0.0,
        help="Seconds to wait before auto-closing a submitted demo initial order.",
    )
    parser.add_argument("--cycle-id", default=None)
    parser.add_argument("--source-signal-id", default=None)
    parser.add_argument("--strategy", default="tick_martingale_probe")
    parser.add_argument("--timeframe", default="TICK")
    parser.add_argument("--base-volume", type=float, default=0.01)
    parser.add_argument("--multiplier", type=float, default=2.0)
    parser.add_argument("--max-steps", type=int, default=2)
    parser.add_argument("--max-total-volume", type=float, default=None)
    parser.add_argument("--max-next-volume", type=float, default=None)
    parser.add_argument("--step-distance-points", type=float, default=80.0)
    parser.add_argument("--recovery-target-points", type=float, default=10.0)
    parser.add_argument("--min-step-interval-ms", type=int, default=0)
    parser.add_argument("--min-tick-count", type=int, default=50)
    parser.add_argument("--min-tick-coverage", type=float, default=0.95)
    parser.add_argument("--max-blocked-ratio", type=float, default=0.05)
    parser.add_argument("--allow-open-replay", action="store_true")
    return parser


def _canary_policy_from_risk_config(risk_config: Any) -> RecoveryExecutionCanaryPolicy:
    return RecoveryExecutionCanaryPolicy.from_config(
        getattr(risk_config, "recovery_execution_canary", None)
    )


def _validate_demo_canary_policy(policy: RecoveryExecutionCanaryPolicy) -> list[str]:
    errors: list[str] = []
    if not policy.enabled:
        errors.append("recovery_canary_not_enabled")
    if not policy.dry_run:
        errors.append("recovery_canary_cli_requires_dry_run")
    if (
        str(policy.order_kind or "market").strip().lower() == "market"
        and not policy.protective_stop_points
    ):
        errors.append("protective_stop_points_required")
    return errors


def _initial_policy_for_live_cycle(
    policy: RecoveryExecutionCanaryPolicy,
    *,
    submit_initial_order: bool,
) -> RecoveryExecutionCanaryPolicy:
    return replace(policy, dry_run=not bool(submit_initial_order))


def _recovery_step_policy_for_live_cycle(
    policy: RecoveryExecutionCanaryPolicy,
    *,
    submit_recovery_order: bool,
) -> RecoveryExecutionCanaryPolicy:
    return replace(policy, dry_run=not bool(submit_recovery_order))


def _validate_live_cycle_submit_request(
    args: argparse.Namespace,
    recovery_policy: RecoveryPolicy,
) -> list[str]:
    errors: list[str] = []
    if not bool(getattr(args, "submit_recovery_order", False)):
        return errors
    if not bool(getattr(args, "live_cycle", False)):
        errors.append("submit_recovery_order_requires_live_cycle")
    if not bool(getattr(args, "submit_initial_order", False)):
        errors.append("submit_recovery_order_requires_submit_initial_order")
    if bool(getattr(args, "keep_initial_open", False)):
        errors.append("submit_recovery_order_requires_auto_cleanup")
    if int(getattr(args, "max_steps", recovery_policy.max_steps)) != 1 or recovery_policy.max_steps != 1:
        errors.append("submit_recovery_order_requires_max_steps_1")
    if float(recovery_policy.base_volume) > 0.01:
        errors.append("submit_recovery_order_base_volume_above_0_01")
    if recovery_policy.max_next_volume is not None and float(recovery_policy.max_next_volume) > 0.02:
        errors.append("submit_recovery_order_next_volume_above_0_02")
    if float(recovery_policy.max_total_volume) > 0.03:
        errors.append("submit_recovery_order_total_volume_above_0_03")
    return errors


def _recovery_policy_from_args(
    args: argparse.Namespace,
    risk_config: Any,
    symbol_info: Any,
) -> RecoveryPolicy:
    base_volume = float(args.base_volume)
    max_total_volume = _bounded_volume(
        requested=args.max_total_volume,
        risk_limit=_float_or_none(getattr(risk_config, "max_volume_per_symbol", None)),
        conservative_default=0.03,
        field="max_total_volume",
    )
    max_next_volume = _bounded_volume(
        requested=args.max_next_volume,
        risk_limit=_float_or_none(getattr(risk_config, "max_volume_per_order", None)),
        conservative_default=round(base_volume * float(args.multiplier), 8),
        field="max_next_volume",
    )
    return RecoveryPolicy(
        enabled=True,
        base_volume=base_volume,
        multiplier=float(args.multiplier),
        max_steps=int(args.max_steps),
        max_total_volume=max_total_volume,
        max_next_volume=max_next_volume,
        step_distance_points=float(args.step_distance_points),
        recovery_target_points=float(args.recovery_target_points),
        point=float(getattr(symbol_info, "point", 0.01) or 0.01),
        min_step_interval_ms=int(args.min_step_interval_ms),
        volume_step=float(getattr(symbol_info, "volume_step", 0.01) or 0.01),
        contract_size=float(
            getattr(symbol_info, "trade_contract_size", 100.0) or 100.0
        ),
    )


def _bounded_volume(
    *,
    requested: float | None,
    risk_limit: float | None,
    conservative_default: float,
    field: str,
) -> float:
    if requested is not None:
        value = float(requested)
        if risk_limit is not None and value > risk_limit:
            raise ValueError(f"{field} exceeds risk limit: {value} > {risk_limit}")
        return value
    candidates = [float(conservative_default)]
    if risk_limit is not None and risk_limit > 0:
        candidates.append(float(risk_limit))
    return min(candidates)


def _gate_policy_from_args(args: argparse.Namespace) -> RecoveryCanaryGatePolicy:
    return RecoveryCanaryGatePolicy(
        min_tick_count=int(args.min_tick_count),
        min_tick_coverage=float(args.min_tick_coverage),
        max_blocked_ratio=float(args.max_blocked_ratio),
        require_closed=not bool(args.allow_open_replay),
    )


def _parse_start(args: argparse.Namespace) -> datetime | None:
    if args.start:
        parsed = datetime.fromisoformat(str(args.start).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    if int(args.lookback_seconds) <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(seconds=int(args.lookback_seconds))


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        parsed = float(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _entry_price_from_quote(quote: Quote, direction: str) -> float:
    normalized = str(direction or "").strip().lower()
    if normalized == "buy":
        return float(quote.ask)
    if normalized == "sell":
        return float(quote.bid)
    raise ValueError("direction must be buy or sell")


def _tick_from_quote(quote: Quote) -> Tick:
    return Tick(
        symbol=quote.symbol,
        bid=quote.bid,
        ask=quote.ask,
        last=quote.last,
        volume=quote.volume,
        time=quote.time,
        time_msc=int(quote.time.timestamp() * 1000),
        flags=0,
    )


def _cycle_payload(cycle: Any) -> dict[str, Any]:
    return {
        "cycle_id": cycle.cycle_id,
        "account_key": cycle.account_key,
        "symbol": cycle.symbol,
        "direction": cycle.direction,
        "status": cycle.status,
        "base_volume": cycle.base_volume,
        "total_volume": cycle.total_volume,
        "step_count": cycle.step_count,
        "average_entry_price": cycle.average_entry_price,
        "last_entry_price": cycle.last_entry_price,
        "started_at": cycle.started_at,
        "updated_at": cycle.updated_at,
        "last_step_at": cycle.last_step_at,
        "strategy": cycle.strategy,
        "timeframe": cycle.timeframe,
        "source_signal_id": cycle.source_signal_id,
        "metadata": dict(cycle.metadata),
    }


def _public_initial_result(result: dict[str, Any]) -> dict[str, Any]:
    payload = dict(result)
    cycle = payload.get("cycle")
    if cycle is not None:
        payload["cycle"] = _cycle_payload(cycle)
    return payload


def _live_cycle_ok(result: dict[str, Any]) -> bool:
    critical_alerts = result.get("critical_alerts")
    if isinstance(critical_alerts, list) and critical_alerts:
        return False
    if result.get("status") not in {"submitted", "dry_run", "evaluated"}:
        return False
    initial = result.get("initial") if isinstance(result.get("initial"), dict) else {}
    if initial.get("status") not in {"submitted", "dry_run"}:
        return False
    if bool(result.get("cleanup_required")):
        cleanup = (
            result.get("initial_cleanup")
            if isinstance(result.get("initial_cleanup"), dict)
            else {}
        )
        recovery_cleanup = (
            result.get("recovery_cleanup")
            if isinstance(result.get("recovery_cleanup"), dict)
            else {}
        )
        if cleanup.get("status") != "closed" and recovery_cleanup.get("status") != "closed":
            return False
    current_step = (
        result.get("current_step") if isinstance(result.get("current_step"), dict) else {}
    )
    if initial.get("status") == "dry_run":
        finalization = (
            result.get("dry_run_cycle_finalization")
            if isinstance(result.get("dry_run_cycle_finalization"), dict)
            else {}
        )
        if finalization.get("status") != "closed":
            return False
    if current_step.get("status") in {"dry_run", "hold", "skipped"}:
        return True
    if current_step.get("status") == "submitted":
        recovery_cleanup = (
            result.get("recovery_cleanup")
            if isinstance(result.get("recovery_cleanup"), dict)
            else {}
        )
        return recovery_cleanup.get("status") == "closed"
    if current_step.get("status") == "block" and current_step.get("reason") in {
        "max_steps_reached",
        "max_next_volume_exceeded",
        "max_total_volume_exceeded",
    }:
        return True
    return False


def _build_runtime_ports(
    *,
    db: TimescaleWriter,
    risk_config: Any,
    economic_config: Any,
    mt5_settings: Any,
    identity: Any,
) -> tuple[TradingModule, TradingStateStore]:
    frequency_provider = TradeCommandAuditFrequencyProvider(
        db,
        account_key=identity.account_key,
        account_alias=identity.account_alias,
    )
    registry = TradingAccountRegistry(
        settings=mt5_settings,
        risk_config=risk_config,
        economic_config=economic_config,
        economic_calendar_service=None,
        trade_frequency_provider=frequency_provider,
        account_key=identity.account_key,
    )
    trading_module = TradingModule(
        registry=registry,
        db_writer=db,
        active_account_alias=identity.account_alias,
    )
    state_store = TradingStateStore(
        db,
        account_alias_getter=lambda: identity.account_alias,
        account_key_getter=lambda: identity.account_key,
    )
    return trading_module, state_store


def _blocked_payload(reason: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"status": "blocked", "reason": reason, "details": dict(details or {})}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.environment != "demo":
        print(
            json.dumps(
                _blocked_payload("recovery_canary_cli_demo_only"),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    set_current_environment(args.environment)
    set_current_instance_name(args.instance)
    reload_configs()

    risk_config = get_risk_config()
    canary_policy = _canary_policy_from_risk_config(risk_config)
    policy_errors = _validate_demo_canary_policy(canary_policy)
    if policy_errors:
        print(
            json.dumps(
                _blocked_payload(
                    "invalid_recovery_canary_policy",
                    details={"errors": policy_errors},
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    ensure_mt5_session_gate_or_raise(
        instance_name=args.instance,
        auto_launch_terminal=bool(args.auto_launch_terminal),
    )

    mt5_settings = load_mt5_settings(instance_name=args.instance)
    market = MT5MarketClient(settings=mt5_settings)
    trading_config = get_trading_config()
    symbol = str(args.symbol or trading_config.default_symbol).strip()
    symbol_info = market.get_symbol_info(symbol)

    recovery_policy = _recovery_policy_from_args(args, risk_config, symbol_info)
    submit_errors = _validate_live_cycle_submit_request(args, recovery_policy)
    if submit_errors:
        print(
            json.dumps(
                _blocked_payload(
                    "invalid_recovery_canary_submit_request",
                    details={"errors": submit_errors},
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    gate_policy = _gate_policy_from_args(args)
    identity = get_runtime_identity()
    cycle_id = str(args.cycle_id or f"demo-recovery-canary-{int(datetime.now(timezone.utc).timestamp() * 1000)}")
    source_signal_id = str(args.source_signal_id or f"{cycle_id}-signal")

    db = TimescaleWriter(load_db_settings("demo"), min_conn=1, max_conn=3)
    try:
        trading_module, state_store = _build_runtime_ports(
            db=db,
            risk_config=risk_config,
            economic_config=get_economic_config(),
            mt5_settings=mt5_settings,
            identity=identity,
        )
        if args.live_cycle:
            initial_quote = market.get_quote(symbol)
            initial = open_initial_recovery_cycle(
                symbol=symbol,
                direction=args.direction,
                entry_price=_entry_price_from_quote(initial_quote, args.direction),
                recovery_policy=recovery_policy,
                canary_policy=_initial_policy_for_live_cycle(
                    canary_policy,
                    submit_initial_order=bool(args.submit_initial_order),
                ),
                account_alias=identity.account_alias,
                account_key=identity.account_key,
                cycle_id=cycle_id,
                source_signal_id=source_signal_id,
                state_store=state_store,
                trading_port=trading_module,
                strategy=args.strategy,
                timeframe=args.timeframe,
            )
            if initial.get("status") in {"submitted", "dry_run"} and initial.get("cycle") is not None:
                current_step = evaluate_current_recovery_step(
                    cycle=initial["cycle"],
                    tick=_tick_from_quote(market.get_quote(symbol)),
                    recovery_policy=recovery_policy,
                    canary_policy=_recovery_step_policy_for_live_cycle(
                        canary_policy,
                        submit_recovery_order=bool(args.submit_recovery_order),
                    ),
                    trading_port=trading_module,
                    state_store=state_store,
                )
            else:
                current_step = {
                    "status": "skipped",
                    "reason": "initial_cycle_not_open",
                }
            if initial.get("status") == "dry_run":
                dry_run_cycle_finalization = close_dry_run_recovery_cycle(
                    cycle=initial.get("cycle"),
                    state_store=state_store,
                    status_reason="canary_dry_run_cycle_completed",
                )
            else:
                dry_run_cycle_finalization = {
                    "status": "skipped",
                    "reason": "initial_not_dry_run",
                }
            cleanup_required = (
                bool(args.submit_initial_order)
                and not bool(args.keep_initial_open)
                and initial.get("status") == "submitted"
            )
            recovery_cleanup = {
                "status": "skipped",
                "reason": "submit_recovery_order_not_requested",
            }
            if cleanup_required and bool(args.submit_recovery_order):
                delay_seconds = max(0.0, min(float(args.initial_cleanup_delay_seconds), 60.0))
                if delay_seconds > 0:
                    time.sleep(delay_seconds)
                recovery_cleanup = close_recovery_canary_positions(
                    initial_result=initial,
                    current_step_result=current_step,
                    cycle_id=cycle_id,
                    source_signal_id=source_signal_id,
                    trading_port=trading_module,
                    deviation=int(canary_policy.deviation),
                    cycle=initial.get("cycle"),
                    state_store=state_store,
                )
                initial_cleanup = {
                    "status": "skipped",
                    "reason": "covered_by_recovery_cleanup",
                }
                if recovery_cleanup.get("status") == "closed":
                    initial_cleanup_verification = {
                        "status": "skipped",
                        "reason": "recovery_cleanup_closed",
                    }
                else:
                    initial_cleanup_verification = verify_initial_recovery_cleanup(
                        cleanup_result=recovery_cleanup,
                        cycle=initial.get("cycle"),
                        cycle_id=cycle_id,
                        source_signal_id=source_signal_id,
                        state_store=state_store,
                        position_reader=trading_module,
                    )
            elif cleanup_required:
                delay_seconds = max(0.0, min(float(args.initial_cleanup_delay_seconds), 60.0))
                if delay_seconds > 0:
                    time.sleep(delay_seconds)
                initial_cleanup = close_initial_recovery_cycle(
                    initial_result=initial,
                    cycle_id=cycle_id,
                    source_signal_id=source_signal_id,
                    trading_port=trading_module,
                    deviation=int(canary_policy.deviation),
                    cycle=initial.get("cycle"),
                    state_store=state_store,
                )
                if initial_cleanup.get("status") == "closed":
                    initial_cleanup_verification = {
                        "status": "skipped",
                        "reason": "cleanup_closed",
                    }
                else:
                    initial_cleanup_verification = verify_initial_recovery_cleanup(
                        cleanup_result=initial_cleanup,
                        cycle=initial.get("cycle"),
                        cycle_id=cycle_id,
                        source_signal_id=source_signal_id,
                        state_store=state_store,
                        position_reader=trading_module,
                    )
            elif bool(args.submit_initial_order) and bool(args.keep_initial_open):
                initial_cleanup = {
                    "status": "skipped",
                    "reason": "cleanup_disabled",
                }
                initial_cleanup_verification = {
                    "status": "skipped",
                    "reason": "cleanup_disabled",
                }
            else:
                initial_cleanup = {
                    "status": "skipped",
                    "reason": "initial_not_submitted",
                }
                initial_cleanup_verification = {
                    "status": "skipped",
                    "reason": "initial_not_submitted",
                }
            critical_alerts = []
            if isinstance(initial_cleanup_verification, dict):
                alert = initial_cleanup_verification.get("alert")
                if isinstance(alert, dict):
                    critical_alerts.append(alert)
            result = {
                "status": (
                    initial.get("status")
                    if current_step.get("status") == "dry_run"
                    else ("blocked" if initial.get("status") == "blocked" else "evaluated")
                ),
                "mode": "live_cycle",
                "cycle_id": cycle_id,
                "source_signal_id": source_signal_id,
                "account_alias": identity.account_alias,
                "account_key": identity.account_key,
                "cleanup_required": cleanup_required,
                "initial": _public_initial_result(initial),
                "current_step": current_step,
                "dry_run_cycle_finalization": dry_run_cycle_finalization,
                "initial_cleanup": initial_cleanup,
                "recovery_cleanup": recovery_cleanup,
                "initial_cleanup_verification": initial_cleanup_verification,
                "critical_alerts": critical_alerts,
            }
        else:
            ticks = market.get_ticks(symbol, int(args.tick_limit), start=_parse_start(args))
            result = execute_recovery_canary(
                symbol=symbol,
                direction=args.direction,
                ticks=ticks,
                recovery_policy=recovery_policy,
                canary_policy=canary_policy,
                gate_policy=gate_policy,
                account_alias=identity.account_alias,
                account_key=identity.account_key,
                cycle_id=cycle_id,
                source_signal_id=source_signal_id,
                state_store=state_store,
                trading_port=trading_module,
                strategy=args.strategy,
                timeframe=args.timeframe,
            )
    finally:
        db.close()

    print(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default))
    if args.live_cycle:
        return 0 if _live_cycle_ok(result) else 1
    return 0 if result.get("status") == "dry_run" else 1


if __name__ == "__main__":
    sys.exit(main())
