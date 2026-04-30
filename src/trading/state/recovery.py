from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from src.signals.metadata_keys import MetadataKey as MK

from ..ports import RecoveryTradingPort, TradeControlStatePort
from ..reasons import REASON_STARTUP_EXPIRED, REASON_STARTUP_OCO_SIBLING_FILLED
from .recovery_policy import TradingStateRecoveryPolicy, _normalize_ticket_set


class TradingStateRecovery:
    """启动阶段的交易状态恢复编排。"""

    def __init__(
        self,
        state_store: Any,
        *,
        policy: TradingStateRecoveryPolicy | None = None,
    ) -> None:
        self._store = state_store
        self._policy = policy or TradingStateRecoveryPolicy(state_store)

    def warm_start(self) -> None:
        self._store.warm_start()

    def restore_trade_control(
        self, trading_module: TradeControlStatePort
    ) -> dict[str, Any]:
        state = self._store.load_trade_control_state()
        if not state:
            return {"restored": False}
        trading_module.apply_trade_control_state(state)
        return {"restored": True}

    def restore_pending_orders(
        self,
        *,
        pending_entry_manager: Any,
        trading_module: RecoveryTradingPort,
    ) -> dict[str, Any]:
        states = list(self._store.list_active_pending_orders())
        try:
            live_orders = list(trading_module.get_orders())
        except Exception as exc:
            return {
                "restored": 0,
                "expired": 0,
                "filled": 0,
                "missing": 0,
                "orphan": 0,
                "error": str(exc),
            }

        local_tickets = {
            int(row.get("order_ticket") or 0)
            for row in states
            if int(row.get("order_ticket") or 0) > 0
        }
        live_by_ticket = {}
        for row in live_orders:
            try:
                ticket = int(self._row_value(row, "ticket", 0) or 0)
            except (TypeError, ValueError):
                continue
            if ticket > 0:
                live_by_ticket[ticket] = row

        summary = {
            "restored": 0,
            "expired": 0,
            "filled": 0,
            "missing": 0,
            "orphan": 0,
            "ignored_missing": 0,
            "orphan_cancelled": 0,
            "oco_sibling_cancelled": 0,
            "oco_groups_restored": 0,
        }

        # ADR-013 P4-residual: 一致性校验——拉同 group 全状态 rows，
        # 找出已有 sibling fill 的 group_ids；这些 group 的 placed rows
        # 必须立即撤（说明上次 fill 后 cancel 未持久化）。
        group_ids_with_placed = {
            str(row.get("order_group_id") or "")
            for row in states
            if row.get("order_group_id")
        }
        groups_with_filled_sibling: set[str] = set()
        if group_ids_with_placed:
            related_rows = self._store.list_pending_orders_by_groups(
                list(group_ids_with_placed)
            )
            for related in related_rows:
                if str(related.get("status") or "").strip().lower() == "filled":
                    gid = str(related.get("order_group_id") or "")
                    if gid:
                        groups_with_filled_sibling.add(gid)

        # 收集需重建的 group 反向索引：group_id → set(entry_keys)
        # 注意：仅 placed 且非 filled-sibling-orphan 的 entry 才入索引。
        group_index_to_restore: dict[str, set[str]] = defaultdict(set)

        for row in states:
            info = self._state_to_pending_info(row)
            order_ticket = int(info.get("ticket", 0) or 0)
            group_id = str(info.get("order_group_id") or "")

            # 一致性校验：本 group 已有 sibling filled → 立即撤本 placed
            if group_id and group_id in groups_with_filled_sibling:
                cancelled = False
                if order_ticket > 0:
                    try:
                        result = trading_module.cancel_orders_by_tickets([order_ticket])
                        cancelled = self._ticket_was_cancelled(result, order_ticket)
                    except Exception:
                        cancelled = False
                self._store.mark_pending_order_cancelled(
                    info, reason=REASON_STARTUP_OCO_SIBLING_FILLED
                )
                summary["oco_sibling_cancelled"] += 1
                if not cancelled:
                    # 即使 broker 撤单未确认，本地状态先标 cancelled；
                    # 后续 reconcile 会再次同步（避免重启后重复触发 fill 链）
                    pass
                continue

            live_order = live_by_ticket.get(order_ticket)
            if live_order is not None:
                expires_at = self._datetime_or_none(info.get("expires_at"))
                if expires_at is not None and datetime.now(timezone.utc) >= expires_at:
                    cancelled = False
                    try:
                        result = trading_module.cancel_orders_by_tickets([order_ticket])
                        cancelled = self._ticket_was_cancelled(result, order_ticket)
                    except Exception:
                        cancelled = False
                    if cancelled:
                        self._store.mark_pending_order_expired(
                            info, reason=REASON_STARTUP_EXPIRED
                        )
                        summary["expired"] += 1
                    else:
                        pending_entry_manager.restore_mt5_order(info)
                        summary["restored"] += 1
                        if group_id:
                            entry_key = str(info.get("signal_id") or "")
                            if entry_key:
                                group_index_to_restore[group_id].add(entry_key)
                    continue

                pending_entry_manager.restore_mt5_order(info)
                summary["restored"] += 1
                if group_id:
                    entry_key = str(info.get("signal_id") or "")
                    if entry_key:
                        group_index_to_restore[group_id].add(entry_key)
                continue

            state = pending_entry_manager.inspect_mt5_order(info)
            status = str((state or {}).get("status") or "missing").strip().lower()
            if status == "filled":
                self._store.mark_pending_order_filled(info, state=state)
                summary["filled"] += 1
            else:
                outcome = self._policy.handle_missing_pending_order(
                    info,
                    state=state,
                )
                summary[outcome] = summary.get(outcome, 0) + 1

        # 重建 _groups 反向索引（仅在所有 restore_mt5_order 完成后批量调用）
        for gid, entry_keys in group_index_to_restore.items():
            pending_entry_manager.restore_group_index(gid, entry_keys)
            summary["oco_groups_restored"] += 1

        for ticket, live_order in live_by_ticket.items():
            if ticket not in local_tickets:
                outcome = self._policy.handle_orphan_pending_order(
                    live_order,
                    trading_module=trading_module,
                )
                summary[outcome] = summary.get(outcome, 0) + 1

        return summary

    @staticmethod
    def _row_value(row: Any, field: str, default: Any = None) -> Any:
        if isinstance(row, dict):
            return row.get(field, default)
        return getattr(row, field, default)

    @staticmethod
    def _datetime_or_none(value: Any):
        from src.trading.state import TradingStateStore

        return TradingStateStore._datetime_or_none(value)

    @staticmethod
    def _ticket_was_cancelled(result: Any, ticket: int) -> bool:
        # P2 回归：standard cancel_orders_by_tickets schema 是
        # {"canceled": [int_ticket, ...], "failed": [{"ticket": int, "error": str}]}
        # 旧实现假设 failed 是 list[int]，对 dict 调 int(item) 抛 TypeError →
        # 上游 except 吞或直接打挂 startup recovery（参 §0s）
        if isinstance(result, dict):
            cancelled = _normalize_ticket_set(
                result.get("canceled") or result.get("cancelled") or []
            )
            failed = _normalize_ticket_set(result.get("failed") or [])
            if ticket in cancelled:
                return True
            if failed:
                return ticket not in failed
        return bool(result)

    @staticmethod
    def _state_to_pending_info(row: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(row.get("metadata") or {})
        params_meta = dict(metadata.get(MK.PARAMS) or {})
        params = None
        if params_meta:
            from src.trading.execution.sizing import TradeParameters

            entry_price = float(
                params_meta.get("entry_price") or row.get("trigger_price") or 0.0
            )
            stop_loss = float(
                params_meta.get("stop_loss") or row.get("stop_loss") or entry_price
            )
            take_profit = float(
                params_meta.get("take_profit") or row.get("take_profit") or entry_price
            )
            sl_distance = abs(entry_price - stop_loss)
            tp_distance = abs(take_profit - entry_price)
            params = TradeParameters(
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                position_size=float(
                    params_meta.get("position_size") or row.get("volume") or 0.0
                ),
                risk_reward_ratio=(
                    (tp_distance / sl_distance) if sl_distance > 0 else 0.0
                ),
                atr_value=float(
                    params_meta.get("atr_value") or row.get("atr_at_entry") or 0.0
                ),
                sl_distance=sl_distance,
                tp_distance=tp_distance,
            )

        return {
            "ticket": row.get("order_ticket"),
            "signal_id": row.get("signal_id"),
            "expires_at": row.get("expires_at"),
            "direction": row.get("direction"),
            "symbol": row.get("symbol"),
            "strategy": row.get("strategy"),
            "timeframe": row.get("timeframe"),
            "confidence": row.get("confidence"),
            "regime": row.get("regime"),
            "comment": row.get("comment") or "",
            "params": params,
            "entry_low": row.get("entry_low"),
            "entry_high": row.get("entry_high"),
            "trigger_price": row.get("trigger_price"),
            "entry_price_requested": row.get("entry_price_requested"),
            "stop_loss": row.get("stop_loss"),
            "take_profit": row.get("take_profit"),
            "volume": row.get("volume"),
            "created_at": row.get("created_at"),
            "metadata": metadata,
            # 从 metadata 恢复出场规格（_pending_metadata 序列化时写入）
            "exit_spec": metadata.get(MK.EXIT_SPEC),
            "strategy_category": metadata.get(MK.STRATEGY_CATEGORY) or "",
            # ADR-013 OCO group 字段（DB 列直读，不依赖 metadata）
            "order_group_id": row.get("order_group_id") or "",
            "group_member_id": row.get("group_member_id") or "",
            "group_role": row.get("group_role") or "",
        }
