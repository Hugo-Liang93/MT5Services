from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from src.trading.broker.comment_codec import looks_like_system_trade_comment
from src.trading.ports import TradingQueryPort

logger = logging.getLogger(__name__)


class TradingStateAlerts:
    """独立的交易状态告警评估器。"""

    def __init__(
        self,
        *,
        state_store: Any,
        trading_module: TradingQueryPort,
        account_alias_getter: Optional[Callable[[], str]] = None,
    ) -> None:
        self._state_store = state_store
        self._trading = trading_module
        self._account_alias_getter = account_alias_getter or (lambda: "")

    def summary(self, *, pending_limit: int = 50, position_limit: int = 50) -> dict[str, Any]:
        # P1 §0t：必须区分"无数据"与"数据源失败"。旧实现 _safe_* 把异常吞成空列表，
        # 双数据源故障时所有告警判据全部 false → 把 DB/MT5 双故障误报成 healthy。
        # 新实现：每次调用记录 source_errors，summary() 末尾把失败源转成显式告警。
        source_errors: dict[str, str] = {}
        active_pending = self._safe_pending_states(
            statuses=["placed", "orphan"],
            limit=pending_limit,
            source_errors=source_errors,
        )
        lifecycle_missing = self._safe_pending_states(
            statuses=["missing"],
            limit=pending_limit,
            source_errors=source_errors,
        )
        open_positions = self._safe_position_states(
            statuses=["open"],
            limit=position_limit,
            source_errors=source_errors,
        )
        live_orders = self._safe_live_orders(source_errors=source_errors)
        live_positions = self._safe_live_positions(source_errors=source_errors)

        active_status_counts = self._state_counts(active_pending)
        missing_count = len(lifecycle_missing)
        orphan_count = int(active_status_counts.get("orphan", 0))
        persisted_active_count = len(active_pending)
        persisted_open_count = len(open_positions)
        live_order_count = len(live_orders)
        live_position_count = len(live_positions)
        unmanaged_positions = self._unmanaged_live_positions(
            live_positions=live_positions,
            managed_rows=open_positions,
        )
        unmanaged_tickets = [
            int(item.get("ticket"))
            for item in unmanaged_positions
            if item.get("ticket") is not None
        ]

        alerts: list[dict[str, Any]] = []
        if missing_count > 0:
            alerts.append(
                self._alert(
                    code="pending_missing",
                    severity="critical",
                    message=f"存在 {missing_count} 笔 unresolved missing 挂单状态",
                    details={"missing_count": missing_count},
                )
            )
        if orphan_count > 0:
            alerts.append(
                self._alert(
                    code="pending_orphan",
                    severity="warning",
                    message=f"存在 {orphan_count} 笔 orphan 挂单",
                    details={"orphan_count": orphan_count},
                )
            )
        if persisted_active_count != live_order_count:
            alerts.append(
                self._alert(
                    code="pending_active_mismatch",
                    severity="warning",
                    message="活跃挂单状态与 MT5 实时挂单数量不一致",
                    details={
                        "persisted_active_count": persisted_active_count,
                        "live_order_count": live_order_count,
                    },
                )
            )
        if unmanaged_positions:
            alerts.append(
                self._alert(
                    code="unmanaged_live_positions",
                    severity="warning",
                    message=f"存在 {len(unmanaged_positions)} 笔未纳管实时持仓",
                    details={
                        "managed_open_count": persisted_open_count,
                        "live_position_count": live_position_count,
                        "unmanaged_tickets": unmanaged_tickets,
                    },
                )
            )

        # P1 §0t：把数据源故障转成显式 critical 告警，禁止"DB/MT5 双失败 → healthy"误报。
        if "state_store" in source_errors:
            alerts.append(
                self._alert(
                    code="state_store_unavailable",
                    severity="critical",
                    message="状态存储不可用，告警判据失效",
                    details={"error": source_errors["state_store"]},
                )
            )
        if "trading_module" in source_errors:
            alerts.append(
                self._alert(
                    code="trading_module_unavailable",
                    severity="critical",
                    message="交易模块查询不可用，告警判据失效",
                    details={"error": source_errors["trading_module"]},
                )
            )

        overall_status = "healthy"
        if any(alert["severity"] == "critical" for alert in alerts):
            overall_status = "critical"
        elif alerts:
            overall_status = "warning"

        return {
            "status": overall_status,
            "account_alias": self._account_alias_getter(),
            "alerts": alerts,
            "summary": [
                {
                    "code": alert["code"],
                    "status": "failed",
                    "severity": alert["severity"],
                    "message": alert["message"],
                }
                for alert in alerts
            ],
            "observed": {
                "active_pending_count": persisted_active_count,
                "active_pending_status_counts": active_status_counts,
                "missing_pending_count": missing_count,
                "open_position_count": persisted_open_count,
                "live_order_count": live_order_count,
                "live_position_count": live_position_count,
                "unmanaged_live_position_count": len(unmanaged_positions),
                "unmanaged_live_position_tickets": unmanaged_tickets,
            },
            "source_errors": dict(source_errors),
        }

    def monitoring_summary(self, *, hours: int = 24) -> dict[str, Any]:
        # P3 §0t：旧实现 `del hours` 后无条件返回当前快照，hours 参数完全死。
        # 现阶段 alerts 评估只依赖最新状态而非历史窗口，无法真正"按 hours 聚合"，
        # 但至少必须把 hours 写进 payload，让消费方知道窗口意图，避免静默失真。
        payload = self.summary()
        payload["time_range_hours"] = int(hours)
        return payload

    def _safe_pending_states(
        self,
        *,
        statuses: list[str],
        limit: int,
        source_errors: dict[str, str],
    ) -> list[dict[str, Any]]:
        try:
            return list(
                self._state_store.list_pending_order_states(
                    statuses=statuses,
                    limit=limit,
                )
            )
        except Exception as exc:
            logger.warning(
                "TradingStateAlerts: list_pending_order_states failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            source_errors.setdefault("state_store", f"{type(exc).__name__}: {exc}")
            return []

    def _safe_position_states(
        self,
        *,
        statuses: list[str],
        limit: int,
        source_errors: dict[str, str],
    ) -> list[dict[str, Any]]:
        try:
            return list(
                self._state_store.list_position_runtime_states(
                    statuses=statuses,
                    limit=limit,
                )
            )
        except Exception as exc:
            logger.warning(
                "TradingStateAlerts: list_position_runtime_states failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            source_errors.setdefault("state_store", f"{type(exc).__name__}: {exc}")
            return []

    def _safe_live_orders(self, *, source_errors: dict[str, str]) -> list[Any]:
        try:
            return list(self._trading.get_orders())
        except Exception as exc:
            logger.warning(
                "TradingStateAlerts: trading_module.get_orders failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            source_errors.setdefault(
                "trading_module", f"{type(exc).__name__}: {exc}"
            )
            return []

    def _safe_live_positions(self, *, source_errors: dict[str, str]) -> list[Any]:
        try:
            return list(self._trading.get_positions())
        except Exception as exc:
            logger.warning(
                "TradingStateAlerts: trading_module.get_positions failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            source_errors.setdefault(
                "trading_module", f"{type(exc).__name__}: {exc}"
            )
            return []

    def _unmanaged_live_positions(
        self,
        *,
        live_positions: list[Any],
        managed_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        managed_tickets = {
            int(ticket)
            for ticket in (
                row.get("position_ticket")
                for row in managed_rows
            )
            if ticket is not None
        }
        resolver = getattr(self._trading, "resolve_position_context", None)
        unmanaged: list[dict[str, Any]] = []
        for position in live_positions:
            ticket = self._attr(position, "ticket")
            if ticket is None:
                continue
            if int(ticket) in managed_tickets:
                continue
            comment = str(self._attr(position, "comment") or "").strip()
            magic = self._attr(position, "magic")
            context = None
            if callable(resolver):
                try:
                    context = resolver(ticket=int(ticket), comment=comment, limit=200)
                except Exception:
                    context = None
            if not comment and int(magic or 0) == 0:
                reason = "manual_position"
            elif context is None and not looks_like_system_trade_comment(comment):
                reason = "unsupported_comment"
            else:
                reason = "missing_context"
            unmanaged.append({"ticket": int(ticket), "reason": reason})
        return unmanaged

    @staticmethod
    def _attr(item: Any, key: str) -> Any:
        if isinstance(item, dict):
            return item.get(key)
        return getattr(item, key, None)

    @staticmethod
    def _state_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            status = str(row.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        return counts

    @staticmethod
    def _alert(
        *,
        code: str,
        severity: str,
        message: str,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "code": code,
            "severity": severity,
            "message": message,
            "details": details,
        }
