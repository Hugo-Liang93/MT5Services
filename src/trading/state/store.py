from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from src.signals.metadata_keys import MetadataKey as MK

from ..reasons import (
    REASON_MATCHED_LIVE_POSITION,
    REASON_MATCHED_TRACKED_POSITION,
    REASON_PLACED_BY_EXECUTOR,
    REASON_RECOVERED_FROM_MT5_WITHOUT_LOCAL_STATE,
)
from ..positions.manager import TrackedPosition
from ..trade_events import POSITION_TRACKED
from .models import (
    PendingOrderStateRecord,
    PositionRuntimeStateRecord,
    TradeControlStateRecord,
)


class TradingStateStore:
    """交易运行状态事实源访问层。"""

    def __init__(
        self, db_writer: Any, *, account_alias_getter: Callable[[], str]
    ) -> None:
        self._db = db_writer
        self._account_alias_getter = account_alias_getter
        self._position_cache: dict[int, dict[str, Any]] = {}
        self._pending_cache: dict[int, dict[str, Any]] = {}
        self._pending_by_signal: dict[str, set[int]] = {}

    def warm_start(self) -> None:
        account_alias = self._account_alias_getter()
        rows = self._db.fetch_position_runtime_states(
            account_alias=account_alias,
            statuses=["open"],
            limit=5000,
        )
        self._position_cache = {
            int(row["position_ticket"]): dict(row)
            for row in rows
            if int(row.get("position_ticket") or 0) > 0
        }
        pending_rows = self._db.fetch_pending_order_states(
            account_alias=account_alias,
            statuses=["placed", "missing"],
            limit=5000,
        )
        self._pending_cache = {}
        self._pending_by_signal = {}
        for row in pending_rows:
            self._cache_pending_row(dict(row))

    def resolve_position_state(self, ticket: int) -> Optional[dict[str, Any]]:
        state = self._position_cache.get(int(ticket))
        return dict(state) if state is not None else None

    def load_trade_control_state(self) -> Optional[dict[str, Any]]:
        return self._db.fetch_trade_control_state(
            account_alias=self._account_alias_getter()
        )

    def list_active_pending_orders(self) -> list[dict[str, Any]]:
        return self._db.fetch_pending_order_states(
            account_alias=self._account_alias_getter(),
            statuses=["placed"],
            limit=5000,
        )

    def list_pending_order_states(
        self,
        *,
        statuses: Optional[list[str]] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self._db.fetch_pending_order_states(
            account_alias=self._account_alias_getter(),
            statuses=statuses,
            limit=limit,
        )

    def list_position_runtime_states(
        self,
        *,
        statuses: Optional[list[str]] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self._db.fetch_position_runtime_states(
            account_alias=self._account_alias_getter(),
            statuses=statuses,
            limit=limit,
        )

    def record_pending_order_placed(self, info: dict[str, Any]) -> None:
        now = self._now()
        record = PendingOrderStateRecord(
            account_alias=self._account_alias_getter(),
            order_ticket=int(info.get("ticket", 0) or 0),
            signal_id=self._str_or_none(info.get("signal_id")),
            request_id=self._str_or_none(info.get("signal_id")),
            symbol=str(info.get("symbol") or ""),
            direction=str(info.get("direction") or ""),
            strategy=str(info.get("strategy") or ""),
            timeframe=str(info.get("timeframe") or ""),
            category=self._metadata_value(info, "category"),
            order_kind=self._metadata_value(info, "order_kind"),
            comment=str(info.get("comment") or ""),
            entry_low=self._float_or_none(info.get("entry_low")),
            entry_high=self._float_or_none(info.get("entry_high")),
            trigger_price=self._float_or_none(info.get("trigger_price")),
            entry_price_requested=self._float_or_none(
                info.get("entry_price_requested")
            ),
            stop_loss=self._float_or_none(info.get("stop_loss")),
            take_profit=self._float_or_none(info.get("take_profit")),
            volume=self._float_or_none(info.get("volume")),
            atr_at_entry=self._params_value(info, "atr_value")
            or self._float_or_none(info.get("atr_at_entry")),
            confidence=self._float_or_none(info.get("confidence")),
            regime=self._str_or_none(info.get("regime")),
            created_at=self._datetime_or_none(info.get("created_at")) or now,
            expires_at=self._datetime_or_none(info.get("expires_at")),
            status="placed",
            status_reason=REASON_PLACED_BY_EXECUTOR,
            last_seen_at=now,
            metadata=self._pending_metadata(info),
            updated_at=now,
        )
        self._db.write_pending_order_states([record.to_row()])
        self._cache_pending_record(record)

    def mark_pending_order_filled(
        self, info: dict[str, Any], *, state: Optional[dict[str, Any]] = None
    ) -> None:
        now = self._now()
        state = dict(state or {})
        record = PendingOrderStateRecord(
            **self._pending_state_from_info(
                info,
                status="filled",
                status_reason=str(state.get("reason") or REASON_MATCHED_LIVE_POSITION),
                updated_at=now,
            ),
            filled_at=now,
            position_ticket=self._int_or_none(state.get("ticket")),
            fill_price=self._float_or_none(state.get("fill_price")),
            last_seen_at=now,
        )
        self._db.write_pending_order_states([record.to_row()])
        self._cache_pending_record(record)

    def mark_pending_order_expired(self, info: dict[str, Any], *, reason: str) -> None:
        now = self._now()
        record = PendingOrderStateRecord(
            **self._pending_state_from_info(
                info, status="expired", status_reason=reason, updated_at=now
            ),
            cancelled_at=now,
            last_seen_at=now,
        )
        self._db.write_pending_order_states([record.to_row()])
        self._cache_pending_record(record)

    def mark_pending_order_cancelled(
        self, info: dict[str, Any], *, reason: str
    ) -> None:
        now = self._now()
        record = PendingOrderStateRecord(
            **self._pending_state_from_info(
                info, status="cancelled", status_reason=reason, updated_at=now
            ),
            cancelled_at=now,
            last_seen_at=now,
        )
        self._db.write_pending_order_states([record.to_row()])
        self._cache_pending_record(record)

    def mark_pending_order_missing(self, info: dict[str, Any], *, reason: str) -> None:
        now = self._now()
        record = PendingOrderStateRecord(
            **self._pending_state_from_info(
                info, status="missing", status_reason=reason, updated_at=now
            ),
            last_seen_at=now,
        )
        self._db.write_pending_order_states([record.to_row()])
        self._cache_pending_record(record)

    def mark_pending_order_orphan(self, order_row: Any) -> None:
        now = self._now()
        order_ticket = self._int_or_none(self._row_value(order_row, "ticket"))
        if order_ticket is None or order_ticket <= 0:
            return
        record = PendingOrderStateRecord(
            account_alias=self._account_alias_getter(),
            order_ticket=order_ticket,
            signal_id=None,
            request_id=None,
            symbol=str(self._row_value(order_row, "symbol", "") or ""),
            direction=self._direction_from_order(order_row),
            comment=str(self._row_value(order_row, "comment", "") or ""),
            entry_price_requested=self._float_or_none(
                self._row_value(order_row, "price_open", None)
            ),
            volume=self._float_or_none(
                self._row_value(order_row, "volume_current", None)
            )
            or self._float_or_none(self._row_value(order_row, "volume_initial", None)),
            created_at=self._datetime_or_none(
                self._row_value(order_row, "time_setup", None)
            ),
            status="orphan",
            status_reason=REASON_RECOVERED_FROM_MT5_WITHOUT_LOCAL_STATE,
            last_seen_at=now,
            metadata={},
            updated_at=now,
        )
        self._db.write_pending_order_states([record.to_row()])
        self._cache_pending_record(record)

    def record_position_tracked(
        self, pos: TrackedPosition, reason: str = POSITION_TRACKED
    ) -> None:
        existing = self._position_cache.get(int(pos.ticket)) or {}
        pending_row = self._match_pending_for_position(pos)
        if pending_row is not None:
            pending_info = self._pending_info_from_row(pending_row)
            self.mark_pending_order_filled(
                pending_info,
                state={
                    "ticket": int(pos.ticket),
                    "fill_price": pos.entry_price,
                    "reason": REASON_MATCHED_TRACKED_POSITION,
                },
            )
            pending_row = (
                self._pending_cache.get(int(pending_info.get("ticket") or 0))
                or pending_row
            )
        now = self._now()
        record = PositionRuntimeStateRecord(
            account_alias=self._account_alias_getter(),
            position_ticket=int(pos.ticket),
            signal_id=self._str_or_none(pos.signal_id),
            order_ticket=(
                self._int_or_none(existing.get("order_ticket"))
                or self._int_or_none((pending_row or {}).get("order_ticket"))
            ),
            symbol=pos.symbol,
            direction=pos.action,
            timeframe=pos.timeframe,
            strategy=pos.strategy,
            comment=pos.comment,
            entry_price=pos.entry_price,
            initial_stop_loss=self._float_or_none(existing.get("initial_stop_loss"))
            or pos.stop_loss,
            initial_take_profit=self._float_or_none(existing.get("initial_take_profit"))
            or pos.take_profit,
            current_stop_loss=pos.stop_loss,
            current_take_profit=pos.take_profit,
            volume=pos.volume,
            atr_at_entry=pos.atr_at_entry,
            confidence=pos.confidence,
            regime=pos.regime,
            opened_at=pos.opened_at,
            last_seen_at=now,
            last_managed_at=now if reason != POSITION_TRACKED else None,
            highest_price=pos.highest_price,
            lowest_price=pos.lowest_price,
            current_price=pos.current_price,
            breakeven_applied=bool(pos.breakeven_applied),
            trailing_active=bool(pos.trailing_active),
            status="open",
            metadata={"source": pos.source, "reason": reason},
            updated_at=now,
        )
        self._db.write_position_runtime_states([record.to_row()])
        self._position_cache[int(pos.ticket)] = self._row_from_position_record(record)

    def record_position_update(self, pos: TrackedPosition, reason: str) -> None:
        self.record_position_tracked(pos, reason=reason)

    def mark_position_closed(
        self, pos: TrackedPosition, close_price: Optional[float]
    ) -> None:
        existing = self._position_cache.get(int(pos.ticket)) or {}
        now = self._now()
        record = PositionRuntimeStateRecord(
            account_alias=self._account_alias_getter(),
            position_ticket=int(pos.ticket),
            signal_id=self._str_or_none(pos.signal_id),
            order_ticket=self._int_or_none(existing.get("order_ticket")),
            symbol=pos.symbol,
            direction=pos.action,
            timeframe=pos.timeframe,
            strategy=pos.strategy,
            comment=pos.comment,
            entry_price=pos.entry_price,
            initial_stop_loss=self._float_or_none(existing.get("initial_stop_loss"))
            or pos.stop_loss,
            initial_take_profit=self._float_or_none(existing.get("initial_take_profit"))
            or pos.take_profit,
            current_stop_loss=pos.stop_loss,
            current_take_profit=pos.take_profit,
            volume=pos.volume,
            atr_at_entry=pos.atr_at_entry,
            confidence=pos.confidence,
            regime=pos.regime,
            opened_at=pos.opened_at,
            last_seen_at=now,
            last_managed_at=now,
            highest_price=pos.highest_price,
            lowest_price=pos.lowest_price,
            current_price=pos.current_price,
            breakeven_applied=bool(pos.breakeven_applied),
            trailing_active=bool(pos.trailing_active),
            status="closed",
            closed_at=now,
            close_source=pos.close_source,
            close_price=close_price,
            metadata={"source": pos.source, "reason": "position_closed"},
            updated_at=now,
        )
        self._db.write_position_runtime_states([record.to_row()])
        self._position_cache.pop(int(pos.ticket), None)

    def sync_trade_control(self, state: dict[str, Any]) -> None:
        record = TradeControlStateRecord(
            account_alias=self._account_alias_getter(),
            auto_entry_enabled=bool(state.get("auto_entry_enabled", True)),
            close_only_mode=bool(state.get("close_only_mode", False)),
            updated_at=self._datetime_or_none(state.get("updated_at")) or self._now(),
            reason=self._str_or_none(state.get("reason")),
            metadata={},
        )
        self._db.write_trade_control_states([record.to_row()])

    def _pending_state_from_info(
        self,
        info: dict[str, Any],
        *,
        status: str,
        status_reason: str,
        updated_at: datetime,
    ) -> dict[str, Any]:
        return dict(
            account_alias=self._account_alias_getter(),
            order_ticket=int(info.get("ticket", 0) or 0),
            signal_id=self._str_or_none(info.get("signal_id")),
            request_id=self._str_or_none(info.get("signal_id")),
            symbol=str(info.get("symbol") or ""),
            direction=str(info.get("direction") or ""),
            strategy=str(info.get("strategy") or ""),
            timeframe=str(info.get("timeframe") or ""),
            category=self._metadata_value(info, "category"),
            order_kind=self._metadata_value(info, "order_kind"),
            comment=str(info.get("comment") or ""),
            entry_low=self._float_or_none(info.get("entry_low")),
            entry_high=self._float_or_none(info.get("entry_high")),
            trigger_price=self._float_or_none(info.get("trigger_price")),
            entry_price_requested=self._float_or_none(
                info.get("entry_price_requested")
            ),
            stop_loss=self._float_or_none(info.get("stop_loss")),
            take_profit=self._float_or_none(info.get("take_profit")),
            volume=self._float_or_none(info.get("volume")),
            atr_at_entry=self._params_value(info, "atr_value"),
            confidence=self._float_or_none(info.get("confidence")),
            regime=self._str_or_none(info.get("regime")),
            created_at=self._datetime_or_none(info.get("created_at")),
            expires_at=self._datetime_or_none(info.get("expires_at")),
            status=status,
            status_reason=status_reason,
            metadata=self._pending_metadata(info),
            updated_at=updated_at,
        )

    def _match_pending_for_position(
        self, pos: TrackedPosition
    ) -> Optional[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        signal_id = self._str_or_none(pos.signal_id)
        if signal_id:
            for ticket in sorted(self._pending_by_signal.get(signal_id, set())):
                row = self._pending_cache.get(ticket)
                if row is not None:
                    candidates.append(row)
        if not candidates:
            row = self._pending_cache.get(int(pos.ticket))
            if row is not None:
                candidates.append(row)
        if not candidates and signal_id:
            signal_id_matches = [
                row
                for row in self._pending_cache.values()
                if self._str_or_none(row.get("signal_id")) == signal_id
            ]
            candidates.extend(signal_id_matches)
        if not candidates:
            return None
        statuses = {"placed", "missing"}
        candidates = [
            row
            for row in candidates
            if str(row.get("status") or "").strip().lower() in statuses
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda row: (
                self._datetime_or_none(row.get("updated_at"))
                or datetime.min.replace(tzinfo=timezone.utc),
                int(row.get("order_ticket") or 0),
            )
        )
        return dict(candidates[-1])

    def _cache_pending_record(self, record: PendingOrderStateRecord) -> None:
        self._cache_pending_row(
            {
                "order_ticket": record.order_ticket,
                "signal_id": record.signal_id,
                "request_id": record.request_id,
                "symbol": record.symbol,
                "direction": record.direction,
                "strategy": record.strategy,
                "timeframe": record.timeframe,
                "category": record.category,
                "order_kind": record.order_kind,
                "comment": record.comment,
                "entry_low": record.entry_low,
                "entry_high": record.entry_high,
                "trigger_price": record.trigger_price,
                "entry_price_requested": record.entry_price_requested,
                "stop_loss": record.stop_loss,
                "take_profit": record.take_profit,
                "volume": record.volume,
                "atr_at_entry": record.atr_at_entry,
                "confidence": record.confidence,
                "regime": record.regime,
                "created_at": record.created_at,
                "expires_at": record.expires_at,
                "filled_at": record.filled_at,
                "cancelled_at": record.cancelled_at,
                "position_ticket": record.position_ticket,
                "deal_id": record.deal_id,
                "fill_price": record.fill_price,
                "status": record.status,
                "status_reason": record.status_reason,
                "last_seen_at": record.last_seen_at,
                "metadata": dict(record.metadata),
                "updated_at": record.updated_at,
            }
        )

    def _cache_pending_row(self, row: dict[str, Any]) -> None:
        ticket = self._int_or_none(row.get("order_ticket"))
        if ticket is None or ticket <= 0:
            return
        previous = self._pending_cache.get(ticket)
        if previous is not None:
            previous_signal = self._str_or_none(previous.get("signal_id"))
            if previous_signal:
                signal_tickets = self._pending_by_signal.get(previous_signal)
                if signal_tickets is not None:
                    signal_tickets.discard(ticket)
                    if not signal_tickets:
                        self._pending_by_signal.pop(previous_signal, None)
        status = str(row.get("status") or "").strip().lower()
        if status not in {"placed", "missing", "orphan"}:
            self._pending_cache.pop(ticket, None)
            return
        cached = dict(row)
        self._pending_cache[ticket] = cached
        signal_id = self._str_or_none(cached.get("signal_id"))
        if signal_id:
            self._pending_by_signal.setdefault(signal_id, set()).add(ticket)

    @staticmethod
    def _pending_info_from_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "ticket": row.get("order_ticket"),
            "signal_id": row.get("signal_id"),
            "symbol": row.get("symbol"),
            "direction": row.get("direction"),
            "strategy": row.get("strategy"),
            "timeframe": row.get("timeframe"),
            "comment": row.get("comment"),
            "entry_low": row.get("entry_low"),
            "entry_high": row.get("entry_high"),
            "trigger_price": row.get("trigger_price"),
            "entry_price_requested": row.get("entry_price_requested"),
            "stop_loss": row.get("stop_loss"),
            "take_profit": row.get("take_profit"),
            "volume": row.get("volume"),
            "confidence": row.get("confidence"),
            "regime": row.get("regime"),
            "created_at": row.get("created_at"),
            "expires_at": row.get("expires_at"),
            "metadata": dict(row.get("metadata") or {}),
        }

    @staticmethod
    def _pending_metadata(info: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(info.get("metadata") or {})
        params = info.get("params")
        if params is not None:
            metadata[MK.PARAMS] = {
                "entry_price": getattr(params, "entry_price", None),
                "stop_loss": getattr(params, "stop_loss", None),
                "take_profit": getattr(params, "take_profit", None),
                "position_size": getattr(params, "position_size", None),
                "atr_value": getattr(params, "atr_value", None),
            }
        # 保留出场规格和策略类别，以便重启恢复后 fill 仍能走正确的出场 profile
        exit_spec = info.get("exit_spec")
        if exit_spec is not None:
            metadata[MK.EXIT_SPEC] = dict(exit_spec)
        strategy_category = info.get("strategy_category")
        if strategy_category:
            metadata[MK.STRATEGY_CATEGORY] = str(strategy_category)
        return metadata

    @staticmethod
    def _row_from_position_record(record: PositionRuntimeStateRecord) -> dict[str, Any]:
        return {
            "position_ticket": record.position_ticket,
            "signal_id": record.signal_id,
            "order_ticket": record.order_ticket,
            "symbol": record.symbol,
            "direction": record.direction,
            "timeframe": record.timeframe,
            "strategy": record.strategy,
            "comment": record.comment,
            "entry_price": record.entry_price,
            "initial_stop_loss": record.initial_stop_loss,
            "initial_take_profit": record.initial_take_profit,
            "current_stop_loss": record.current_stop_loss,
            "current_take_profit": record.current_take_profit,
            "volume": record.volume,
            "atr_at_entry": record.atr_at_entry,
            "confidence": record.confidence,
            "regime": record.regime,
            "opened_at": record.opened_at,
            "last_seen_at": record.last_seen_at,
            "last_managed_at": record.last_managed_at,
            "highest_price": record.highest_price,
            "lowest_price": record.lowest_price,
            "current_price": record.current_price,
            "breakeven_applied": record.breakeven_applied,
            "trailing_active": record.trailing_active,
            "status": record.status,
            "closed_at": record.closed_at,
            "close_source": record.close_source,
            "close_price": record.close_price,
            "metadata": dict(record.metadata),
            "updated_at": record.updated_at,
        }

    @staticmethod
    def _row_value(row: Any, field: str, default: Any = None) -> Any:
        if isinstance(row, dict):
            return row.get(field, default)
        return getattr(row, field, default)

    @staticmethod
    def _direction_from_order(order_row: Any) -> str:
        try:
            order_type = int(TradingStateStore._row_value(order_row, "type", 0) or 0)
        except (TypeError, ValueError):
            return ""
        return "sell" if order_type in {1, 3, 5, 7} else "buy"

    @staticmethod
    def _params_value(info: dict[str, Any], field: str) -> Optional[float]:
        params = info.get("params")
        if params is None:
            return None
        return TradingStateStore._float_or_none(getattr(params, field, None))

    @staticmethod
    def _metadata_value(info: dict[str, Any], field: str) -> str:
        metadata = info.get("metadata")
        if isinstance(metadata, dict) and metadata.get(field) is not None:
            return str(metadata.get(field))
        return ""

    @staticmethod
    def _datetime_or_none(value: Any) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        return None

    @staticmethod
    def _float_or_none(value: Any) -> Optional[float]:
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _int_or_none(value: Any) -> Optional[int]:
        try:
            return None if value is None else int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _str_or_none(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
