from __future__ import annotations

import logging
import math
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

from src.config import get_shared_default_symbol
from src.trading.execution.reasons import REASON_QUOTE_STALE
from src.trading.execution.quote_health import build_execution_quote_health
from src.trading.runtime.lifecycle import OwnedThreadLifecycle

from .models import AccountRiskStateRecord

logger = logging.getLogger(__name__)


def _coerce_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _sanitize_jsonable(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _sanitize_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_jsonable(item) for item in value]
    return value


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


class AccountRiskStateProjector:
    """账户自治风控的正式当前态投影。

    只读取当前账户实例的本地状态机与共享只读上下文，不做任何远程控制。
    """

    def __init__(
        self,
        *,
        write_fn,
        runtime_identity,
        trade_module,
        trade_executor,
        position_manager,
        pending_entry_manager,
        runtime_mode_controller=None,
        market_service=None,
        projection_interval_seconds: float = 5.0,
    ) -> None:
        self._write_fn = write_fn
        self._runtime_identity = runtime_identity
        self._trade_module = trade_module
        self._trade_executor = trade_executor
        self._position_manager = position_manager
        self._pending_entry_manager = pending_entry_manager
        self._runtime_mode_controller = runtime_mode_controller
        self._market_service = market_service
        self._projection_interval_seconds = max(1.0, float(projection_interval_seconds))
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lifecycle = OwnedThreadLifecycle(
            self,
            "_thread",
            label="AccountRiskStateProjector",
        )
        self._lock = threading.RLock()
        self._last_snapshot: dict[str, Any] | None = None
        self._last_error: str | None = None
        self._db_degraded: bool = False
        self._last_projected_at: str | None = None

    def start(self) -> None:
        if self._lifecycle.is_running():
            return
        self._stop_event.clear()
        self.project_now()
        self._lifecycle.ensure_running(
            lambda: threading.Thread(
                target=self._run,
                name="account-risk-projector",
                daemon=True,
            )
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._lifecycle.stop(self._stop_event, timeout=timeout)

    def is_running(self) -> bool:
        return self._lifecycle.is_running()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.is_running(),
                "projection_interval_seconds": self._projection_interval_seconds,
                "last_projected_at": self._last_projected_at,
                "last_error": self._last_error,
                "db_degraded": self._db_degraded,
                "snapshot": dict(self._last_snapshot or {}),
            }

    def project_now(self) -> dict[str, Any] | None:
        snapshot = self.build_snapshot()
        record = self._build_record(snapshot)
        try:
            self._write_fn([record.to_row()])
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
                self._db_degraded = True
            logger.warning("AccountRiskStateProjector: persist failed", exc_info=True)
            return None
        with self._lock:
            self._last_snapshot = snapshot
            self._last_error = None
            self._db_degraded = False
            self._last_projected_at = snapshot["updated_at"]
        return dict(snapshot)

    def build_snapshot(self) -> dict[str, Any]:
        control = _coerce_dict(self._trade_module.trade_control_status())
        executor_status = _coerce_dict(self._trade_executor.status())
        position_status = _coerce_dict(self._position_manager.status())
        pending_status = (
            _coerce_dict(self._pending_entry_manager.status())
            if self._pending_entry_manager is not None
            else {}
        )
        runtime_mode = None
        if self._runtime_mode_controller is not None:
            runtime_mode = str(
                _coerce_dict(self._runtime_mode_controller.snapshot()).get("current_mode") or ""
            ).strip() or None

        circuit_breaker = _coerce_dict(executor_status.get("circuit_breaker"))
        margin_guard = _coerce_dict(position_status.get("margin_guard"))
        margin_level = self._resolve_margin_level(margin_guard)
        if margin_level is None:
            margin_level = self._resolve_margin_level_from_account()

        open_positions_count = _coerce_int(position_status.get("tracked_positions"))
        pending_orders_count = _coerce_int(pending_status.get("active_count"))
        quote_health = build_execution_quote_health(
            self._market_service,
            self._representative_symbol(),
        )
        quote_stale = bool(quote_health.get("stale", False))
        indicator_degraded = bool(position_status.get("last_error"))
        last_risk_block = str(executor_status.get("last_risk_block") or "").strip() or None

        active_flags: list[str] = []
        if runtime_mode in {"risk_off", "ingest_only"}:
            active_flags.append(f"runtime_mode:{runtime_mode}")
        if not bool(control.get("auto_entry_enabled", True)):
            active_flags.append("auto_entry_paused")
        if bool(control.get("close_only_mode")):
            active_flags.append("close_only_mode")
        if bool(circuit_breaker.get("open")):
            active_flags.append("execution_circuit_open")
        if bool(margin_guard.get("should_block_new_trades")):
            active_flags.append("margin_block_new_trades")
        if bool(margin_guard.get("should_tighten_stops")):
            active_flags.append("margin_tighten_stops")
        if bool(margin_guard.get("should_emergency_close")):
            active_flags.append("margin_emergency_close")
        if quote_stale:
            active_flags.append("quote_stale")
        if indicator_degraded:
            active_flags.append("indicator_degraded")
        should_block_new_trades = any(
            (
                runtime_mode in {"risk_off", "ingest_only"},
                bool(control.get("close_only_mode")),
                bool(circuit_breaker.get("open")),
                bool(margin_guard.get("should_block_new_trades")),
                quote_stale,
            )
        )
        if quote_stale and not last_risk_block:
            last_risk_block = REASON_QUOTE_STALE

        updated_at = datetime.now(timezone.utc).isoformat()
        return {
            "account_key": self._runtime_identity.account_key,
            "account_alias": self._runtime_identity.account_alias,
            "instance_id": self._runtime_identity.instance_id,
            "instance_role": self._runtime_identity.instance_role,
            "runtime_mode": runtime_mode,
            "auto_entry_enabled": bool(control.get("auto_entry_enabled", True)),
            "close_only_mode": bool(control.get("close_only_mode", False)),
            "circuit_open": bool(circuit_breaker.get("open", False)),
            "consecutive_failures": _coerce_int(
                circuit_breaker.get("consecutive_failures")
            ),
            "last_risk_block": last_risk_block,
            "margin_level": margin_level,
            "margin_guard_state": str(margin_guard.get("state") or "").strip() or None,
            "should_block_new_trades": should_block_new_trades,
            "should_tighten_stops": bool(margin_guard.get("should_tighten_stops", False)),
            "should_emergency_close": bool(margin_guard.get("should_emergency_close", False)),
            "open_positions_count": open_positions_count,
            "pending_orders_count": pending_orders_count,
            "quote_stale": quote_stale,
            "indicator_degraded": indicator_degraded,
            "db_degraded": False,
            "active_risk_flags": active_flags,
            "updated_at": updated_at,
            "metadata": {
                "trade_control": {
                    "reason": control.get("reason"),
                    "actor": control.get("actor"),
                    "updated_at": control.get("updated_at"),
                },
                "circuit_breaker": circuit_breaker,
                "margin_guard": margin_guard,
                "position_manager": {
                    "reconcile_count": position_status.get("reconcile_count"),
                    "last_reconcile_at": position_status.get("last_reconcile_at"),
                    "last_error": position_status.get("last_error"),
                },
                "pending_entries": {
                    "active_count": pending_orders_count,
                },
                "quote_health": {
                    "age_seconds": quote_health.get("age_seconds"),
                    "stale_threshold_seconds": quote_health.get("stale_threshold_seconds"),
                },
            },
        }

    def _build_record(self, snapshot: dict[str, Any]) -> AccountRiskStateRecord:
        return AccountRiskStateRecord(
            account_key=snapshot["account_key"],
            account_alias=snapshot["account_alias"],
            instance_id=snapshot["instance_id"],
            instance_role=snapshot["instance_role"],
            runtime_mode=snapshot.get("runtime_mode"),
            auto_entry_enabled=bool(snapshot.get("auto_entry_enabled", True)),
            close_only_mode=bool(snapshot.get("close_only_mode", False)),
            circuit_open=bool(snapshot.get("circuit_open", False)),
            consecutive_failures=_coerce_int(snapshot.get("consecutive_failures")),
            last_risk_block=str(snapshot.get("last_risk_block") or "").strip() or None,
            margin_level=_coerce_float(snapshot.get("margin_level")),
            margin_guard_state=str(snapshot.get("margin_guard_state") or "").strip() or None,
            should_block_new_trades=bool(snapshot.get("should_block_new_trades", False)),
            should_tighten_stops=bool(snapshot.get("should_tighten_stops", False)),
            should_emergency_close=bool(snapshot.get("should_emergency_close", False)),
            open_positions_count=_coerce_int(snapshot.get("open_positions_count")),
            pending_orders_count=_coerce_int(snapshot.get("pending_orders_count")),
            quote_stale=bool(snapshot.get("quote_stale", False)),
            indicator_degraded=bool(snapshot.get("indicator_degraded", False)),
            db_degraded=bool(snapshot.get("db_degraded", False)),
            active_risk_flags=list(snapshot.get("active_risk_flags") or []),
            metadata=_sanitize_jsonable(_coerce_dict(snapshot.get("metadata"))),
            updated_at=datetime.now(timezone.utc),
        )

    def _resolve_margin_level(self, margin_guard: dict[str, Any]) -> float | None:
        return _coerce_float(margin_guard.get("margin_level"))

    def _resolve_margin_level_from_account(self) -> float | None:
        try:
            info = self._trade_module.account_info()
        except Exception:
            logger.debug("AccountRiskStateProjector: account_info failed", exc_info=True)
            return None
        if info is None:
            return None
        if isinstance(info, dict):
            direct = _coerce_float(info.get("margin_level"))
            if direct is not None:
                return direct
            equity = _coerce_float(info.get("equity"))
            margin = _coerce_float(info.get("margin"))
        else:
            direct = _coerce_float(getattr(info, "margin_level", None))
            if direct is not None:
                return direct
            equity = _coerce_float(getattr(info, "equity", None))
            margin = _coerce_float(getattr(info, "margin", None))
        if equity is None or margin in (None, 0.0):
            return None
        return round((equity / margin) * 100.0, 2)

    def _representative_symbol(self) -> str | None:
        symbols: list[str] = []
        try:
            positions: Iterable[dict[str, Any]] = list(self._position_manager.active_positions())
        except Exception:
            positions = []
        for item in positions:
            if isinstance(item, dict):
                symbol = str(item.get("symbol") or "").strip()
            else:
                symbol = str(getattr(item, "symbol", "") or "").strip()
            if symbol:
                symbols.append(symbol)
        if self._pending_entry_manager is not None:
            try:
                pending = list(self._pending_entry_manager.active_execution_contexts())
            except Exception:
                pending = []
            for item in pending:
                symbol = str(item.get("symbol") or "").strip()
                if symbol:
                    symbols.append(symbol)
        if symbols:
            return symbols[0]
        return get_shared_default_symbol()

    def _run(self) -> None:
        while not self._stop_event.wait(timeout=self._projection_interval_seconds):
            self.project_now()
