from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, List, Optional, Sequence, Tuple

from src.persistence.schema import (
    UPSERT_PENDING_ORDER_STATES_SQL,
    UPSERT_POSITION_RUNTIME_STATES_SQL,
    UPSERT_TRADE_CONTROL_STATE_SQL,
)

if TYPE_CHECKING:
    from src.persistence.db import TimescaleWriter


class TradingStateRepository:
    def __init__(self, writer: "TimescaleWriter"):
        self._writer = writer

    def write_pending_order_states(self, rows: Iterable[Tuple], page_size: int = 200) -> None:
        batch = []
        for row in rows:
            metadata = row[31] if row[31] is not None else {}
            batch.append((*row[:31], self._writer._json(metadata), row[32]))
        if not batch:
            return
        self._writer._batch(UPSERT_PENDING_ORDER_STATES_SQL, batch, page_size=page_size)

    def fetch_pending_order_states(
        self,
        *,
        account_alias: Optional[str] = None,
        statuses: Optional[Sequence[str]] = None,
        limit: int = 500,
    ) -> List[dict]:
        sql = (
            "SELECT account_alias, order_ticket, signal_id, request_id, symbol, direction, "
            "strategy, timeframe, category, order_kind, comment, "
            "entry_low, entry_high, trigger_price, entry_price_requested, "
            "stop_loss, take_profit, volume, atr_at_entry, confidence, regime, "
            "created_at, expires_at, filled_at, cancelled_at, "
            "position_ticket, deal_id, fill_price, status, status_reason, "
            "last_seen_at, metadata, updated_at "
            "FROM pending_order_states WHERE 1=1"
        )
        params: List = []
        if account_alias is not None:
            sql += " AND account_alias = %s"
            params.append(account_alias)
        if statuses:
            placeholders = ", ".join(["%s"] * len(statuses))
            sql += f" AND status IN ({placeholders})"
            params.extend(list(statuses))
        sql += " ORDER BY updated_at DESC LIMIT %s"
        params.append(limit)
        return self._fetch_dicts(sql, params)

    def write_position_runtime_states(self, rows: Iterable[Tuple], page_size: int = 200) -> None:
        batch = []
        for row in rows:
            metadata = row[30] if row[30] is not None else {}
            batch.append((*row[:30], self._writer._json(metadata), row[31]))
        if not batch:
            return
        self._writer._batch(UPSERT_POSITION_RUNTIME_STATES_SQL, batch, page_size=page_size)

    def fetch_position_runtime_states(
        self,
        *,
        account_alias: Optional[str] = None,
        statuses: Optional[Sequence[str]] = None,
        limit: int = 500,
    ) -> List[dict]:
        sql = (
            "SELECT account_alias, position_ticket, signal_id, order_ticket, symbol, direction, "
            "timeframe, strategy, comment, entry_price, "
            "initial_stop_loss, initial_take_profit, current_stop_loss, current_take_profit, "
            "volume, atr_at_entry, confidence, regime, "
            "opened_at, last_seen_at, last_managed_at, "
            "highest_price, lowest_price, current_price, "
            "breakeven_applied, trailing_active, "
            "status, closed_at, close_source, close_price, metadata, updated_at "
            "FROM position_runtime_states WHERE 1=1"
        )
        params: List = []
        if account_alias is not None:
            sql += " AND account_alias = %s"
            params.append(account_alias)
        if statuses:
            placeholders = ", ".join(["%s"] * len(statuses))
            sql += f" AND status IN ({placeholders})"
            params.extend(list(statuses))
        sql += " ORDER BY updated_at DESC LIMIT %s"
        params.append(limit)
        return self._fetch_dicts(sql, params)

    def write_trade_control_states(self, rows: Iterable[Tuple], page_size: int = 50) -> None:
        batch = []
        for row in rows:
            metadata = row[5] if row[5] is not None else {}
            batch.append((*row[:5], self._writer._json(metadata)))
        if not batch:
            return
        self._writer._batch(UPSERT_TRADE_CONTROL_STATE_SQL, batch, page_size=page_size)

    def fetch_trade_control_state(self, *, account_alias: str) -> Optional[dict]:
        rows = self._fetch_dicts(
            "SELECT account_alias, auto_entry_enabled, close_only_mode, updated_at, reason, metadata "
            "FROM trade_control_state WHERE account_alias = %s",
            [account_alias],
        )
        return rows[0] if rows else None

    def _fetch_dicts(self, sql: str, params: Sequence) -> List[dict]:
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in rows]
