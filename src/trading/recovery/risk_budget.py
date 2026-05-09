from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


@dataclass(frozen=True)
class RecoveryRiskBudgetSettings:
    risk_profile: str = "recovery_budgeted"
    max_daily_recovery_loss_amount: float = 0.0
    max_rolling_recovery_loss_amount: float = 0.0
    rolling_loss_window_minutes: int = 60
    max_consecutive_loss_cycles: int = 0
    loss_lockout_minutes: int = 0

    @property
    def enabled(self) -> bool:
        return bool(
            self.max_daily_recovery_loss_amount > 0
            or self.max_rolling_recovery_loss_amount > 0
            or self.max_consecutive_loss_cycles > 0
        )


@dataclass(frozen=True)
class RecoveryRiskBudgetDecision:
    allowed: bool
    reason: str
    snapshot: dict[str, Any]


class RecoveryRiskBudgetGuard:
    """Loss-budget admission guard for resident recovery cycles."""

    def assess(
        self,
        *,
        settings: RecoveryRiskBudgetSettings,
        state_store: Any,
        now: datetime,
        account_key: str,
        symbol: str,
        strategy: str,
        dry_run: bool,
    ) -> RecoveryRiskBudgetDecision:
        now = _aware_utc(now)
        normalized_account_key = str(account_key or "").strip()
        snapshot = _base_snapshot(settings, account_key=normalized_account_key)
        if not settings.enabled:
            return RecoveryRiskBudgetDecision(
                allowed=True,
                reason="recovery_risk_budget_disabled",
                snapshot=snapshot,
            )

        rows_result = self._load_rows(
            state_store=state_store,
            settings=settings,
            account_key=normalized_account_key,
            symbol=symbol,
            strategy=strategy,
            dry_run=dry_run,
        )
        if isinstance(rows_result, str):
            snapshot["active_reason"] = "recovery_budget_state_unavailable"
            snapshot["error"] = rows_result
            return RecoveryRiskBudgetDecision(
                allowed=False,
                reason="recovery_budget_state_unavailable",
                snapshot=snapshot,
            )

        rows = rows_result
        metrics = _loss_metrics(
            rows=rows,
            now=now,
            rolling_window_minutes=int(settings.rolling_loss_window_minutes),
        )
        snapshot.update(metrics)
        daily_limit = float(settings.max_daily_recovery_loss_amount or 0.0)
        rolling_limit = float(settings.max_rolling_recovery_loss_amount or 0.0)
        snapshot["remaining_daily_loss_amount"] = _remaining(
            daily_limit,
            metrics["daily_realized_loss_amount"],
        )
        snapshot["remaining_rolling_loss_amount"] = _remaining(
            rolling_limit,
            metrics["rolling_realized_loss_amount"],
        )

        if daily_limit > 0 and metrics["daily_realized_loss_amount"] >= daily_limit:
            snapshot["active_reason"] = "recovery_daily_loss_budget_reached"
            snapshot["remaining_daily_loss_amount"] = 0.0
            return RecoveryRiskBudgetDecision(
                allowed=False,
                reason="recovery_daily_loss_budget_reached",
                snapshot=snapshot,
            )

        if (
            rolling_limit > 0
            and metrics["rolling_realized_loss_amount"] >= rolling_limit
        ):
            snapshot["active_reason"] = "recovery_rolling_loss_budget_reached"
            snapshot["remaining_rolling_loss_amount"] = 0.0
            return RecoveryRiskBudgetDecision(
                allowed=False,
                reason="recovery_rolling_loss_budget_reached",
                snapshot=snapshot,
            )

        lockout = _consecutive_loss_lockout(
            rows=rows,
            now=now,
            max_consecutive_loss_cycles=int(settings.max_consecutive_loss_cycles),
            loss_lockout_minutes=int(settings.loss_lockout_minutes),
        )
        snapshot.update(lockout["snapshot"])
        if lockout["blocked"]:
            snapshot["active_reason"] = "recovery_consecutive_loss_lockout"
            return RecoveryRiskBudgetDecision(
                allowed=False,
                reason="recovery_consecutive_loss_lockout",
                snapshot=snapshot,
            )

        return RecoveryRiskBudgetDecision(
            allowed=True,
            reason="recovery_risk_budget_ok",
            snapshot=snapshot,
        )

    @staticmethod
    def _load_rows(
        *,
        state_store: Any,
        settings: RecoveryRiskBudgetSettings,
        account_key: str,
        symbol: str,
        strategy: str,
        dry_run: bool,
    ) -> list[Mapping[str, Any]] | str:
        lister = getattr(state_store, "list_recovery_cycle_states", None)
        if not callable(lister):
            return "list_recovery_cycle_states port unavailable"
        try:
            rows = list(
                lister(
                    symbol=symbol,
                    strategy=strategy,
                    limit=_history_limit(settings),
                )
                or []
            )
        except Exception as exc:  # noqa: BLE001 - risk budget must fail closed.
            return str(exc)

        filtered: list[Mapping[str, Any]] = []
        for row in rows:
            if not _row_matches_resident_recovery(
                row,
                account_key=account_key,
                symbol=symbol,
                strategy=strategy,
                dry_run=dry_run,
            ):
                continue
            filtered.append(row)
        return filtered


def recovery_row_is_dry_run_cycle(row: Mapping[str, Any]) -> bool:
    metadata = row.get("metadata")
    if not isinstance(metadata, Mapping):
        return False
    initial_result = metadata.get("initial_result")
    if isinstance(initial_result, Mapping) and "dry_run" in initial_result:
        return bool(initial_result.get("dry_run"))
    return False


def _base_snapshot(
    settings: RecoveryRiskBudgetSettings,
    *,
    account_key: str,
) -> dict[str, Any]:
    return {
        "enabled": bool(settings.enabled),
        "profile": str(settings.risk_profile),
        "account_key": str(account_key or ""),
        "max_daily_recovery_loss_amount": float(
            settings.max_daily_recovery_loss_amount or 0.0
        ),
        "max_rolling_recovery_loss_amount": float(
            settings.max_rolling_recovery_loss_amount or 0.0
        ),
        "rolling_loss_window_minutes": int(settings.rolling_loss_window_minutes),
        "max_consecutive_loss_cycles": int(settings.max_consecutive_loss_cycles),
        "loss_lockout_minutes": int(settings.loss_lockout_minutes),
        "daily_realized_loss_amount": 0.0,
        "rolling_realized_loss_amount": 0.0,
        "remaining_daily_loss_amount": _remaining(
            float(settings.max_daily_recovery_loss_amount or 0.0),
            0.0,
        ),
        "remaining_rolling_loss_amount": _remaining(
            float(settings.max_rolling_recovery_loss_amount or 0.0),
            0.0,
        ),
        "consecutive_loss_cycles": 0,
        "loss_lockout_until": None,
        "active_reason": None,
        "error": None,
    }


def _loss_metrics(
    *,
    rows: list[Mapping[str, Any]],
    now: datetime,
    rolling_window_minutes: int,
) -> dict[str, Any]:
    today = now.astimezone(timezone.utc).date()
    rolling_start = now - timedelta(minutes=max(1, int(rolling_window_minutes)))
    daily_loss = 0.0
    rolling_loss = 0.0
    for row in rows:
        loss = _realized_loss(row)
        if loss <= 0:
            continue
        occurred_at = _row_event_time(row)
        if occurred_at is None:
            continue
        occurred_at = _aware_utc(occurred_at)
        if occurred_at.date() == today:
            daily_loss += loss
        if occurred_at >= rolling_start:
            rolling_loss += loss
    return {
        "daily_realized_loss_amount": round(daily_loss, 2),
        "rolling_realized_loss_amount": round(rolling_loss, 2),
    }


def _consecutive_loss_lockout(
    *,
    rows: list[Mapping[str, Any]],
    now: datetime,
    max_consecutive_loss_cycles: int,
    loss_lockout_minutes: int,
) -> dict[str, Any]:
    snapshot = {
        "consecutive_loss_cycles": 0,
        "loss_lockout_until": None,
    }
    if max_consecutive_loss_cycles <= 0:
        return {"blocked": False, "snapshot": snapshot}

    ordered = sorted(
        rows,
        key=lambda row: _row_event_time(row)
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    latest_loss_at: datetime | None = None
    consecutive = 0
    for row in ordered:
        event_at = _row_event_time(row)
        if event_at is None:
            continue
        if _realized_loss(row) <= 0:
            break
        consecutive += 1
        if latest_loss_at is None:
            latest_loss_at = _aware_utc(event_at)

    snapshot["consecutive_loss_cycles"] = consecutive
    if consecutive < max_consecutive_loss_cycles or latest_loss_at is None:
        return {"blocked": False, "snapshot": snapshot}

    if loss_lockout_minutes <= 0:
        return {"blocked": True, "snapshot": snapshot}

    lockout_until = latest_loss_at + timedelta(minutes=loss_lockout_minutes)
    snapshot["loss_lockout_until"] = lockout_until.isoformat()
    return {"blocked": lockout_until > now, "snapshot": snapshot}


def _row_matches_resident_recovery(
    row: Any,
    *,
    account_key: str,
    symbol: str,
    strategy: str,
    dry_run: bool,
) -> bool:
    if not isinstance(row, Mapping):
        return False
    row_account_key = str(row.get("account_key") or "").strip()
    expected_account_key = str(account_key or "").strip()
    if expected_account_key and row_account_key != expected_account_key:
        return False
    if str(row.get("symbol") or "").strip().upper() != str(symbol).strip().upper():
        return False
    if str(row.get("strategy") or "").strip() != str(strategy).strip():
        return False
    status_reason = str(row.get("status_reason") or "").strip()
    if not status_reason.startswith("resident_recovery_"):
        return False
    if not dry_run and recovery_row_is_dry_run_cycle(row):
        return False
    return True


def _realized_loss(row: Mapping[str, Any]) -> float:
    try:
        pnl = float(row.get("realized_pnl"))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, -pnl)


def _row_event_time(row: Mapping[str, Any]) -> datetime | None:
    for key in ("closed_at", "updated_at", "started_at"):
        parsed = _parse_datetime(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _remaining(limit: float, used: float) -> float | None:
    if limit <= 0:
        return None
    return round(max(0.0, float(limit) - float(used)), 2)


def _history_limit(settings: RecoveryRiskBudgetSettings) -> int:
    base = 200
    if settings.max_consecutive_loss_cycles > 0:
        base = max(base, int(settings.max_consecutive_loss_cycles) * 5)
    return base
