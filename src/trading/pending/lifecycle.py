"""PendingEntry 的 MT5 挂单生命周期子模块。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from ..reasons import REASON_MISSING_WITHOUT_FILL, REASON_NEW_SIGNAL_OVERRIDE
logger = logging.getLogger(__name__)


class PendingOrderLifecycleManager:
    """管理 MT5 挂单的状态追踪与取消/过期检查。"""

    def __init__(
        self,
        *,
        lock: Any,
        mt5_orders: Dict[str, Dict[str, Any]],
        cancellation_port: Any,
        inspect_mt5_order_fn: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        stats: Optional[dict[str, Any]] = None,
    ) -> None:
        self._lock = lock
        self._mt5_orders = mt5_orders
        self._cancellation_port = cancellation_port
        self._inspect_mt5_order_fn = inspect_mt5_order_fn
        self._stats = stats if isinstance(stats, dict) else {}
        self._on_tracked: Optional[Callable[[Dict[str, Any]], None]] = None
        self._on_filled: Optional[
            Callable[[Dict[str, Any], Dict[str, Any]], None]
        ] = None
        self._on_expired: Optional[Callable[[Dict[str, Any], str], None]] = None
        self._on_cancelled: Optional[Callable[[Dict[str, Any], str], None]] = None
        self._on_missing: Optional[Callable[[Dict[str, Any], str], None]] = None

    def set_order_lifecycle_hooks(
        self,
        *,
        on_tracked: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_filled: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]] = None,
        on_expired: Optional[Callable[[Dict[str, Any], str], None]] = None,
        on_cancelled: Optional[Callable[[Dict[str, Any], str], None]] = None,
        on_missing: Optional[Callable[[Dict[str, Any], str], None]] = None,
    ) -> None:
        self._on_tracked = on_tracked
        self._on_filled = on_filled
        self._on_expired = on_expired
        self._on_cancelled = on_cancelled
        self._on_missing = on_missing

    def inspect_mt5_order(self, info: Dict[str, Any]) -> Dict[str, Any]:
        inspector = self._inspect_mt5_order_fn
        if not callable(inspector):
            return {"status": "pending"}
        return inspector(dict(info)) or {"status": "pending"}

    def restore_mt5_order(self, info: Dict[str, Any]) -> None:
        restored = dict(info)
        signal_id = str(restored.get("signal_id") or "").strip()
        if not signal_id:
            return
        with self._lock:
            self._mt5_orders[signal_id] = restored

    def track_mt5_order(
        self,
        *,
        signal_id: str,
        order_ticket: int,
        expires_at: datetime,
        direction: str,
        symbol: str,
        strategy: str,
        timeframe: str = "",
        confidence: Optional[float] = None,
        regime: Optional[str] = None,
        comment: str = "",
        params: Optional[Any] = None,
        order_kind: str = "",
        entry_low: Optional[float] = None,
        entry_high: Optional[float] = None,
        trigger_price: Optional[float] = None,
        entry_price_requested: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        volume: Optional[float] = None,
        created_at: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
        exit_spec: Optional[Dict[str, Any]] = None,
        strategy_category: str = "",
    ) -> None:
        tracked_info = {
            "ticket": order_ticket,
            "signal_id": signal_id,
            "expires_at": expires_at,
            "direction": direction,
            "symbol": symbol,
            "strategy": strategy,
            "timeframe": timeframe,
            "confidence": confidence,
            "regime": regime,
            "comment": str(comment or ""),
            "params": params,
            "order_kind": str(order_kind or ""),
            "entry_low": entry_low,
            "entry_high": entry_high,
            "trigger_price": trigger_price,
            "entry_price_requested": entry_price_requested,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "volume": volume,
            "created_at": created_at,
            "metadata": dict(metadata or {}),
            "exit_spec": dict(exit_spec) if exit_spec else None,
            "strategy_category": str(strategy_category or ""),
        }
        with self._lock:
            if signal_id in self._mt5_orders:
                existing_ticket = self._mt5_orders[signal_id].get("ticket")
                logger.warning(
                    "PendingEntry: signal_id=%s already tracked with ticket=%s, "
                    "ignoring duplicate submission with ticket=%d",
                    signal_id,
                    existing_ticket,
                    order_ticket,
                )
                return
            self._mt5_orders[signal_id] = tracked_info
            self._stats["mt5_orders_placed"] = self._stats.get("mt5_orders_placed", 0) + 1

        if self._on_tracked is not None:
            self._on_tracked(dict(tracked_info))
        logger.info(
            "PendingEntry: tracking MT5 order ticket=%d for %s/%s %s (expires=%s)",
            order_ticket,
            symbol,
            strategy,
            direction,
            expires_at.isoformat(),
        )

    def check_mt5_order_state(self) -> None:
        if not callable(self._inspect_mt5_order_fn):
            return

        with self._lock:
            tracked_orders = [
                (sid, dict(info)) for sid, info in self._mt5_orders.items()
            ]

        for sid, info in tracked_orders:
            try:
                state = self.inspect_mt5_order(dict(info))
            except Exception:
                logger.warning(
                    "PendingEntry: inspect_mt5_order_fn failed for signal %s",
                    sid,
                    exc_info=True,
                )
                continue

            status = str(state.get("status") or "pending").strip().lower()
            if status not in {"filled", "missing"}:
                continue

            with self._lock:
                current = self._mt5_orders.get(sid)
                if current is None:
                    continue
                if int(current.get("ticket", 0) or 0) != int(
                    info.get("ticket", 0) or 0
                ):
                    continue
                self._mt5_orders.pop(sid, None)
                if status == "filled":
                    self._stats["mt5_orders_filled"] = (
                        self._stats.get("mt5_orders_filled", 0) + 1
                    )
                else:
                    self._stats["mt5_orders_missing"] = (
                        self._stats.get("mt5_orders_missing", 0) + 1
                    )

            if status == "filled" and self._on_filled is not None:
                self._on_filled(dict(info), dict(state))
            if status == "missing" and self._on_missing is not None:
                self._on_missing(
                    dict(info),
                    str(state.get("reason") or REASON_MISSING_WITHOUT_FILL),
                )

            if status == "filled":
                logger.info(
                    "PendingEntry: MT5 order ticket=%d filled for %s/%s (signal=%s, position=%s)",
                    int(info.get("ticket", 0) or 0),
                    info.get("symbol"),
                    info.get("strategy"),
                    sid[:8],
                    state.get("ticket"),
                )
            else:
                logger.info(
                    "PendingEntry: MT5 order ticket=%d no longer exists for %s/%s "
                    "(signal=%s, reason=%s)",
                    int(info.get("ticket", 0) or 0),
                    info.get("symbol"),
                    info.get("strategy"),
                    sid[:8],
                    state.get("reason") or REASON_MISSING_WITHOUT_FILL,
                )

    def check_mt5_order_expiry(self, on_expired: Optional[Callable[[str, str], None]]) -> None:
        """检查 MT5 挂单是否超时，超时则通过 MT5 API 取消。"""
        now = datetime.now(timezone.utc)
        expired: list[tuple[str, Dict[str, Any]]] = []
        with self._lock:
            for sid, info in list(self._mt5_orders.items()):
                if now >= info["expires_at"]:
                    expired.append((sid, dict(info)))

        for sid, info in expired:
            ticket = info["ticket"]
            cancelled = False
            result: Any = None
            try:
                result = self._cancellation_port.cancel_orders_by_tickets([ticket])
                cancelled = self._ticket_in_result(
                    result, ticket, success_keys=("canceled", "cancelled")
                )
            except Exception:
                logger.error(
                    "PendingEntry: failed to cancel MT5 order ticket=%d",
                    ticket,
                    exc_info=True,
                )
            if cancelled:
                logger.info(
                    "PendingEntry: cancelled expired MT5 order ticket=%d "
                    "for %s/%s (signal=%s)",
                    ticket,
                    info["symbol"],
                    info["strategy"],
                    sid[:8],
                )
                with self._lock:
                    current = self._mt5_orders.get(sid)
                    if current is not None and int(
                        current.get("ticket", 0) or 0
                    ) == int(ticket):
                        self._mt5_orders.pop(sid, None)
                        self._stats["mt5_orders_expired"] = (
                            self._stats.get("mt5_orders_expired", 0) + 1
                        )
                if self._on_expired is not None:
                    self._on_expired(dict(info), "mt5_order_expired")
            if on_expired:
                try:
                    if cancelled:
                        on_expired(sid, "mt5_order_expired")
                except Exception:
                    pass
            elif result is not None:
                logger.warning(
                    "PendingEntry: expiry cancel not confirmed for MT5 order ticket=%d "
                    "for %s/%s (signal=%s, result=%s)",
                    ticket,
                    info["symbol"],
                    info["strategy"],
                    sid[:8],
                    result,
                )

    def cancel_mt5_orders_by_symbol(
        self,
        symbol: str,
        reason: str = REASON_NEW_SIGNAL_OVERRIDE,
        *,
        exclude_direction: Optional[str] = None,
    ) -> int:
        """取消指定品种的所有 MT5 挂单。"""
        to_cancel: list[tuple[str, Dict[str, Any]]] = []
        with self._lock:
            for sid, info in list(self._mt5_orders.items()):
                if info["symbol"] == symbol and (
                    exclude_direction is None
                    or str(info.get("direction") or "") != exclude_direction
                ):
                    to_cancel.append((sid, dict(info)))
        cancelled_count = 0
        for sid, info in to_cancel:
            ticket = info["ticket"]
            try:
                result = self._cancellation_port.cancel_orders_by_tickets([ticket])
                if self._ticket_in_result(
                    result, ticket, success_keys=("canceled", "cancelled")
                ):
                    with self._lock:
                        current = self._mt5_orders.get(sid)
                        if current is not None and int(
                            current.get("ticket", 0) or 0
                        ) == int(ticket):
                            self._mt5_orders.pop(sid, None)
                    cancelled_count += 1
                    if self._on_cancelled is not None:
                        self._on_cancelled(dict(info), reason)
            except Exception:
                logger.error(
                    "Failed to cancel MT5 order ticket=%d", ticket, exc_info=True
                )
        return cancelled_count

    @staticmethod
    def _ticket_in_result(
        result: Any, ticket: int, *, success_keys: tuple[str, ...]
    ) -> bool:
        if isinstance(result, dict):
            success = set()
            for key in success_keys:
                success.update(PendingOrderLifecycleManager._extract_tickets(result.get(key, [])))
            failed = PendingOrderLifecycleManager._extract_tickets(result.get("failed", []))
            if ticket in success:
                return True
            if failed:
                return ticket not in failed
        return bool(result)

    @staticmethod
    def _extract_tickets(items: Any) -> set[int]:
        tickets: set[int] = set()
        for item in list(items or []):
            candidate = item
            if isinstance(item, dict):
                candidate = (
                    item.get("ticket") or item.get("order") or item.get("order_id")
                )
            try:
                normalized = int(candidate)
            except (TypeError, ValueError):
                continue
            if normalized > 0:
                tickets.add(normalized)
        return tickets
