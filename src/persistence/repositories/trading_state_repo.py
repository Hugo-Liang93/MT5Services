from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, List, Optional, Sequence, Tuple

from src.persistence.schema import (
    UPSERT_ACCOUNT_RISK_STATE_SQL,
    UPSERT_PENDING_ORDER_STATES_SQL,
    UPSERT_POSITION_RUNTIME_STATES_SQL,
    UPSERT_TRADE_CONTROL_STATE_SQL,
)

if TYPE_CHECKING:
    from src.persistence.db import TimescaleWriter


class TradingStateRepository:
    def __init__(self, writer: "TimescaleWriter"):
        self._writer = writer

    def write_pending_order_states(
        self, rows: Iterable[Tuple], page_size: int = 200
    ) -> None:
        batch = []
        for row in rows:
            metadata = row[31] if row[31] is not None else {}
            batch.append((*row[:31], self._writer._json(metadata), row[32], row[33]))
        if not batch:
            return
        self._writer._batch(UPSERT_PENDING_ORDER_STATES_SQL, batch, page_size=page_size)

    def fetch_pending_order_states(
        self,
        *,
        account_alias: Optional[str] = None,
        account_key: Optional[str] = None,
        statuses: Optional[Sequence[str]] = None,
        signal_id: Optional[str] = None,
        limit: int = 500,
    ) -> List[dict]:
        sql = (
            "SELECT account_alias, order_ticket, signal_id, request_id, symbol, direction, "
            "strategy, timeframe, category, order_kind, comment, "
            "entry_low, entry_high, trigger_price, entry_price_requested, "
            "stop_loss, take_profit, volume, atr_at_entry, confidence, regime, "
            "created_at, expires_at, filled_at, cancelled_at, "
            "position_ticket, deal_id, fill_price, status, status_reason, "
            "last_seen_at, metadata, updated_at, account_key "
            "FROM pending_order_states WHERE 1=1"
        )
        params: List = []
        if account_key is not None:
            sql += " AND account_key = %s"
            params.append(account_key)
        elif account_alias is not None:
            sql += " AND account_alias = %s"
            params.append(account_alias)
        if signal_id is not None:
            sql += " AND signal_id = %s"
            params.append(signal_id)
        if statuses:
            placeholders = ", ".join(["%s"] * len(statuses))
            sql += f" AND status IN ({placeholders})"
            params.extend(list(statuses))
        sql += " ORDER BY updated_at DESC LIMIT %s"
        params.append(limit)
        return self._fetch_dicts(sql, params)

    def write_position_runtime_states(
        self, rows: Iterable[Tuple], page_size: int = 200
    ) -> None:
        batch = []
        for row in rows:
            metadata = row[30] if row[30] is not None else {}
            batch.append((*row[:30], self._writer._json(metadata), row[31], row[32]))
        if not batch:
            return
        self._writer._batch(
            UPSERT_POSITION_RUNTIME_STATES_SQL, batch, page_size=page_size
        )

    def fetch_position_runtime_states(
        self,
        *,
        account_alias: Optional[str] = None,
        account_key: Optional[str] = None,
        statuses: Optional[Sequence[str]] = None,
        signal_id: Optional[str] = None,
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
            "status, closed_at, close_source, close_price, metadata, updated_at, account_key "
            "FROM position_runtime_states WHERE 1=1"
        )
        params: List = []
        if account_key is not None:
            sql += " AND account_key = %s"
            params.append(account_key)
        elif account_alias is not None:
            sql += " AND account_alias = %s"
            params.append(account_alias)
        if signal_id is not None:
            sql += " AND signal_id = %s"
            params.append(signal_id)
        if statuses:
            placeholders = ", ".join(["%s"] * len(statuses))
            sql += f" AND status IN ({placeholders})"
            params.extend(list(statuses))
        sql += " ORDER BY updated_at DESC LIMIT %s"
        params.append(limit)
        return self._fetch_dicts(sql, params)

    def write_trade_control_states(
        self, rows: Iterable[Tuple], page_size: int = 50
    ) -> None:
        batch = []
        for row in rows:
            metadata = row[5] if row[5] is not None else {}
            batch.append((*row[:5], self._writer._json(metadata), row[6]))
        if not batch:
            return
        self._writer._batch(UPSERT_TRADE_CONTROL_STATE_SQL, batch, page_size=page_size)

    def fetch_trade_control_state(
        self,
        *,
        account_alias: Optional[str] = None,
        account_key: Optional[str] = None,
    ) -> Optional[dict]:
        sql = (
            "SELECT account_alias, auto_entry_enabled, close_only_mode, updated_at, reason, metadata, account_key "
            "FROM trade_control_state WHERE 1=1"
        )
        params: List = []
        if account_key is not None:
            sql += " AND account_key = %s"
            params.append(account_key)
        elif account_alias is not None:
            sql += " AND account_alias = %s"
            params.append(account_alias)
        sql += " LIMIT 1"
        rows = self._fetch_dicts(sql, params)
        if not rows:
            return None
        row = dict(rows[0])
        metadata = row.get("metadata")
        if isinstance(metadata, dict):
            for key, value in metadata.items():
                row.setdefault(str(key), value)
        return row

    def write_account_risk_states(
        self, rows: Iterable[Tuple], page_size: int = 50
    ) -> None:
        batch = []
        for row in rows:
            active_risk_flags = row[20] if row[20] is not None else []
            metadata = row[21] if row[21] is not None else {}
            batch.append(
                (
                    *row[:20],
                    self._writer._json(active_risk_flags),
                    self._writer._json(metadata),
                    row[22],
                )
            )
        if not batch:
            return
        self._writer._batch(UPSERT_ACCOUNT_RISK_STATE_SQL, batch, page_size=page_size)

    def fetch_account_risk_state(
        self,
        *,
        account_alias: Optional[str] = None,
        account_key: Optional[str] = None,
    ) -> Optional[dict]:
        rows = self.fetch_account_risk_states(
            account_alias=account_alias,
            account_key=account_key,
            limit=1,
        )
        return rows[0] if rows else None

    def fetch_account_risk_states(
        self,
        *,
        account_alias: Optional[str] = None,
        account_key: Optional[str] = None,
        limit: int = 500,
    ) -> List[dict]:
        sql = (
            "SELECT account_key, account_alias, instance_id, instance_role, runtime_mode, "
            "auto_entry_enabled, close_only_mode, circuit_open, consecutive_failures, "
            "last_risk_block, margin_level, margin_guard_state, should_block_new_trades, "
            "should_tighten_stops, should_emergency_close, open_positions_count, "
            "pending_orders_count, quote_stale, indicator_degraded, db_degraded, "
            "active_risk_flags, metadata, updated_at "
            "FROM account_risk_state WHERE 1=1"
        )
        params: List = []
        if account_key is not None:
            sql += " AND account_key = %s"
            params.append(account_key)
        elif account_alias is not None:
            sql += " AND account_alias = %s"
            params.append(account_alias)
        sql += " ORDER BY updated_at DESC LIMIT %s"
        params.append(limit)
        return self._fetch_dicts(sql, params)

    def fetch_latest_risk_state_per_account(self) -> List[dict]:
        """P10.1: 所有账户最新 account_risk_state 快照（单条/账户）。"""
        sql = (
            "SELECT DISTINCT ON (account_key) "
            "account_key, account_alias, instance_id, instance_role, runtime_mode, "
            "auto_entry_enabled, close_only_mode, circuit_open, consecutive_failures, "
            "last_risk_block, margin_level, margin_guard_state, should_block_new_trades, "
            "should_tighten_stops, should_emergency_close, open_positions_count, "
            "pending_orders_count, quote_stale, indicator_degraded, db_degraded, "
            "active_risk_flags, metadata, updated_at "
            "FROM account_risk_state "
            "ORDER BY account_key, updated_at DESC"
        )
        return self._fetch_dicts(sql, [])

    def aggregate_open_positions_by_account_symbol(self) -> List[dict]:
        """P10.1: 跨账户持仓聚合（latest-per-ticket → sum per account-symbol-direction）。

        §0s 回归：暴露 latest_updated_at（bucket 内 latest position updated_at
        的 MAX）让 cockpit exposure_map.freshness 真实反映底层数据陈旧度，
        而非聚合时刻 (observed_at) 永远 fresh。
        """
        sql = """
WITH latest AS (
    SELECT DISTINCT ON (account_key, position_ticket)
        account_key,
        account_alias,
        position_ticket,
        symbol,
        direction,
        volume,
        status,
        entry_price,
        current_price,
        updated_at
    FROM position_runtime_states
    ORDER BY account_key, position_ticket, updated_at DESC
)
SELECT account_alias,
       account_key,
       symbol,
       direction,
       SUM(volume)::double precision AS gross_volume,
       COUNT(*)::bigint AS position_count,
       MAX(updated_at) AS latest_updated_at
FROM latest
WHERE status = 'open'
GROUP BY account_alias, account_key, symbol, direction
ORDER BY SUM(volume) DESC
"""
        return self._fetch_dicts(sql, [])

    def aggregate_pending_orders_by_account_symbol(self) -> List[dict]:
        """P12-1: 跨账户 pending 挂单聚合（latest-per-ticket → sum per account-symbol-direction）。

        仅统计仍未成交 / 未撤销 / 未过期的活跃挂单（status in 'placed' / 'pending'）。
        `filled` / `expired` / `cancelled` 排除。给 cockpit exposure_map 的
        pending_exposure 字段供数。
        """
        sql = """
WITH latest AS (
    SELECT DISTINCT ON (account_key, order_ticket)
        account_key,
        account_alias,
        order_ticket,
        symbol,
        direction,
        volume,
        status,
        updated_at
    FROM pending_order_states
    ORDER BY account_key, order_ticket, updated_at DESC
)
SELECT account_alias,
       account_key,
       symbol,
       direction,
       SUM(volume)::double precision AS pending_volume,
       COUNT(*)::bigint AS pending_count,
       MAX(updated_at) AS latest_updated_at
FROM latest
WHERE status IN ('placed', 'pending')
GROUP BY account_alias, account_key, symbol, direction
ORDER BY SUM(volume) DESC
"""
        return self._fetch_dicts(sql, [])

    def _fetch_dicts(self, sql: str, params: Sequence) -> List[dict]:
        with self._writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in rows]
