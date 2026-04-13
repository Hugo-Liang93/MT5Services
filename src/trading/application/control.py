from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable, Optional

from src.trading.reasons import REASON_AUTO_ENTRY_PAUSED, REASON_CLOSE_ONLY_MODE_ENABLED
from src.risk.service import PreTradeRiskBlockedError
from src.signals.metadata_keys import MetadataKey as MK


PRECHECK_TRADE_FIELDS = {
    "symbol",
    "volume",
    "side",
    "order_kind",
    "price",
    "sl",
    "tp",
    "deviation",
    "comment",
    "magic",
    "metadata",
}


class TradeControlStateService:
    """交易控制状态服务。

    负责维护 auto-entry / close-only 状态，并在命令执行前执行控制拦截。
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._state: dict[str, Any] = {
            "auto_entry_enabled": True,
            "close_only_mode": False,
            "updated_at": None,
            "reason": None,
            "actor": None,
            "action_id": None,
            "audit_id": None,
            "idempotency_key": None,
            "request_context": {},
        }
        self._update_hook: Optional[Callable[[dict[str, Any]], None]] = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def set_update_hook(
        self,
        fn: Optional[Callable[[dict[str, Any]], None]],
    ) -> None:
        self._update_hook = fn

    def apply_state(self, state: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._state["auto_entry_enabled"] = bool(
                state.get("auto_entry_enabled", True)
            )
            self._state["close_only_mode"] = bool(
                state.get("close_only_mode", False)
            )
            self._state["reason"] = str(state.get("reason") or "").strip() or None
            self._state["actor"] = str(state.get("actor") or "").strip() or None
            self._state["action_id"] = str(state.get("action_id") or "").strip() or None
            self._state["audit_id"] = str(state.get("audit_id") or "").strip() or None
            self._state["idempotency_key"] = (
                str(state.get("idempotency_key") or "").strip() or None
            )
            request_context = state.get("request_context")
            self._state["request_context"] = (
                dict(request_context) if isinstance(request_context, dict) else {}
            )
            updated_at = state.get("updated_at")
            self._state["updated_at"] = (
                updated_at.isoformat() if isinstance(updated_at, datetime) else updated_at
            )
            return dict(self._state)

    def update(
        self,
        *,
        auto_entry_enabled: Optional[bool] = None,
        close_only_mode: Optional[bool] = None,
        reason: Optional[str] = None,
        actor: Optional[str] = None,
        action_id: Optional[str] = None,
        audit_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        request_context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        with self._lock:
            if auto_entry_enabled is not None:
                self._state["auto_entry_enabled"] = bool(auto_entry_enabled)
            if close_only_mode is not None:
                self._state["close_only_mode"] = bool(close_only_mode)
            self._state["reason"] = (str(reason).strip() or None) if reason is not None else None
            self._state["actor"] = (str(actor).strip() or None) if actor is not None else None
            self._state["action_id"] = (
                str(action_id).strip() or None if action_id is not None else None
            )
            self._state["audit_id"] = (
                str(audit_id).strip() or None if audit_id is not None else None
            )
            self._state["idempotency_key"] = (
                str(idempotency_key).strip() or None
                if idempotency_key is not None
                else None
            )
            self._state["request_context"] = (
                dict(request_context) if isinstance(request_context, dict) else {}
            )
            self._state["updated_at"] = datetime.now(timezone.utc).isoformat()
            snapshot = dict(self._state)
        if self._update_hook is not None:
            self._update_hook(snapshot)
        return snapshot

    @staticmethod
    def entry_origin(payload: dict[str, Any]) -> str:
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        if isinstance(metadata, dict):
            return str(metadata.get(MK.ENTRY_ORIGIN) or "manual").strip().lower()
        return "manual"

    def build_block_assessment(
        self,
        *,
        reason: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "enabled": True,
            "mode": "strict",
            "blocked": True,
            "verdict": "block",
            "reason": reason,
            "symbol": payload.get("symbol"),
            "active_windows": [],
            "upcoming_windows": [],
            "warnings": [],
            "checks": [
                {
                    "name": "trade_control",
                    "verdict": "block",
                    "reason": reason,
                    "details": {
                        "control_state": self.status(),
                        "entry_origin": self.entry_origin(payload),
                    },
                }
            ],
            "intent": {k: v for k, v in payload.items() if k in PRECHECK_TRADE_FIELDS},
        }

    def enforce(self, payload: dict[str, Any]) -> None:
        state = self.status()
        entry_origin = self.entry_origin(payload)
        if state.get("close_only_mode"):
            raise PreTradeRiskBlockedError(
                "trade entry disabled: close_only_mode_enabled",
                assessment=self.build_block_assessment(
                    reason=REASON_CLOSE_ONLY_MODE_ENABLED,
                    payload=payload,
                ),
            )
        if entry_origin == "auto" and not bool(state.get("auto_entry_enabled", True)):
            raise PreTradeRiskBlockedError(
                "trade entry disabled: auto_entry_paused",
                assessment=self.build_block_assessment(
                    reason=REASON_AUTO_ENTRY_PAUSED,
                    payload=payload,
                ),
            )
