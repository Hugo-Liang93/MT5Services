from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from src.risk.rules import TradeFrequencyQuotaExceeded


class TradeCommandAuditFrequencyProvider:
    """Account-scoped trade frequency source backed by persisted command audits."""

    def __init__(
        self,
        db_writer: Any,
        *,
        account_key: str | None = None,
        account_alias: str | None = None,
    ) -> None:
        self._db_writer = db_writer
        self._account_key = str(account_key or "").strip() or None
        self._account_alias = str(account_alias or "").strip() or None
        self._lock = threading.RLock()
        self._local_reservations: dict[str, dict[str, Any]] = {}
        self._committed_reservation_ttl = timedelta(seconds=30)
        self._active_reservation_ttl = timedelta(minutes=5)

    def count_trades_since(
        self,
        since: datetime,
        *,
        account_key: str | None = None,
    ) -> int:
        count_fn = getattr(
            self._db_writer,
            "count_successful_trade_commands_since",
            None,
        )
        if count_fn is None:
            raise RuntimeError("trade command audit count port is unavailable")

        resolved_account_key = str(account_key or self._account_key or "").strip()
        kwargs: dict[str, Any] = {"since": since}
        if resolved_account_key:
            kwargs["account_key"] = resolved_account_key
        elif self._account_alias:
            kwargs["account_alias"] = self._account_alias
        else:
            raise ValueError("trade frequency provider requires an account scope")
        success_count = max(0, int(count_fn(**kwargs)))
        reservation_count_fn = getattr(
            self._db_writer,
            "count_trade_frequency_reservations_since",
            None,
        )
        if reservation_count_fn is not None:
            return success_count + max(0, int(reservation_count_fn(**kwargs)))
        return success_count + self._count_local_reservations_since(
            since,
            account_key=resolved_account_key or self._account_key,
        )

    def reserve_trade_slot(
        self,
        *,
        account_key: str,
        at_time: datetime,
        max_trades_per_day: int | None,
        max_trades_per_hour: int | None,
    ) -> str:
        resolved_account_key = str(account_key or self._account_key or "").strip()
        if not resolved_account_key:
            raise ValueError("trade frequency reservation requires account_key")
        reserve_fn = getattr(self._db_writer, "reserve_trade_frequency_quota", None)
        if reserve_fn is not None:
            try:
                return str(
                    reserve_fn(
                        account_key=resolved_account_key,
                        account_alias=self._account_alias,
                        at_time=at_time,
                        max_trades_per_day=max_trades_per_day,
                        max_trades_per_hour=max_trades_per_hour,
                    )
                )
            except RuntimeError as exc:
                message = str(exc)
                if "trade limit reached" in message.lower():
                    raise TradeFrequencyQuotaExceeded(message) from exc
                raise
        return self._reserve_local_trade_slot(
            account_key=resolved_account_key,
            at_time=at_time,
            max_trades_per_day=max_trades_per_day,
            max_trades_per_hour=max_trades_per_hour,
        )

    def finalize_trade_slot(self, reservation_id: str, *, committed: bool) -> None:
        reservation_id = str(reservation_id or "").strip()
        if not reservation_id:
            return
        finalize_fn = getattr(
            self._db_writer,
            "finalize_trade_frequency_reservation",
            None,
        )
        if finalize_fn is not None:
            finalize_fn(reservation_id=reservation_id, committed=committed)
            return
        with self._lock:
            record = self._local_reservations.get(reservation_id)
            if record is None:
                return
            if committed:
                record["status"] = "committed"
                record["expires_at"] = datetime.now(timezone.utc) + (
                    self._committed_reservation_ttl
                )
            else:
                self._local_reservations.pop(reservation_id, None)

    def _count_local_reservations_since(
        self,
        since: datetime,
        *,
        account_key: str | None,
    ) -> int:
        self._prune_local_reservations()
        resolved_account_key = str(account_key or "").strip()
        with self._lock:
            return sum(
                1
                for item in self._local_reservations.values()
                if item["account_key"] == resolved_account_key
                and item["reserved_at"] >= since
                and item["expires_at"] > datetime.now(timezone.utc)
            )

    def _reserve_local_trade_slot(
        self,
        *,
        account_key: str,
        at_time: datetime,
        max_trades_per_day: int | None,
        max_trades_per_hour: int | None,
    ) -> str:
        self._prune_local_reservations()
        with self._lock:
            if max_trades_per_day is not None:
                day_start = at_time.astimezone(timezone.utc).replace(
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                )
                if (
                    self.count_trades_since(day_start, account_key=account_key)
                    >= max_trades_per_day
                ):
                    raise TradeFrequencyQuotaExceeded("Daily trade limit reached")
            if max_trades_per_hour is not None:
                hour_start = at_time - timedelta(hours=1)
                if (
                    self.count_trades_since(hour_start, account_key=account_key)
                    >= max_trades_per_hour
                ):
                    raise TradeFrequencyQuotaExceeded("Hourly trade limit reached")
            reservation_id = uuid4().hex
            self._local_reservations[reservation_id] = {
                "account_key": account_key,
                "reserved_at": at_time,
                "expires_at": at_time + self._active_reservation_ttl,
                "status": "active",
            }
            return reservation_id

    def _prune_local_reservations(self) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            expired = [
                reservation_id
                for reservation_id, item in self._local_reservations.items()
                if item["expires_at"] <= now
            ]
            for reservation_id in expired:
                self._local_reservations.pop(reservation_id, None)
