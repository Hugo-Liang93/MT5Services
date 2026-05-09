from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from .models import RecoveryCycleState


@dataclass(frozen=True)
class RecoveryExposureSnapshot:
    status: str
    fresh: bool
    submitted_tickets: list[int] = field(default_factory=list)
    live_position_tickets: list[int] = field(default_factory=list)
    pending_submitted_tickets: list[int] = field(default_factory=list)
    position_matches: list[dict[str, Any]] = field(default_factory=list)
    last_reconcile_at: datetime | None = None

    @property
    def absent_confirmed(self) -> bool:
        return self.status == "absent" and self.fresh

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "fresh": bool(self.fresh),
            "submitted_tickets": list(self.submitted_tickets),
            "live_position_tickets": list(self.live_position_tickets),
            "pending_submitted_tickets": list(self.pending_submitted_tickets),
            "position_matches": [dict(item) for item in self.position_matches],
            "last_reconcile_at": _iso(self.last_reconcile_at),
            "absent_confirmed": self.absent_confirmed,
        }


class RecoveryExposureLedger:
    """Classifies submitted recovery tickets against broker position snapshots."""

    def classify(
        self,
        cycle: RecoveryCycleState,
        *,
        positions: list[Mapping[str, Any]] | None,
        last_reconcile_at: datetime | None,
    ) -> RecoveryExposureSnapshot:
        submitted_items = _submitted_tickets(cycle)
        submitted_tickets = [int(item["ticket"]) for item in submitted_items]
        if not submitted_tickets:
            return RecoveryExposureSnapshot(status="none", fresh=True)

        if positions is None:
            return RecoveryExposureSnapshot(
                status="pending",
                fresh=False,
                submitted_tickets=submitted_tickets,
                pending_submitted_tickets=submitted_tickets,
                last_reconcile_at=last_reconcile_at,
            )

        fresh = last_reconcile_at is not None and _datetime_after_or_equal(
            last_reconcile_at, cycle.updated_at
        )
        matches = _matching_recovery_positions(cycle, list(positions))
        live_tickets = _unique_positive_ints(
            _position_ticket(position) for position in matches
        )
        if live_tickets:
            return RecoveryExposureSnapshot(
                status="confirmed",
                fresh=fresh,
                submitted_tickets=submitted_tickets,
                live_position_tickets=live_tickets,
                pending_submitted_tickets=[
                    ticket for ticket in submitted_tickets if ticket not in live_tickets
                ],
                position_matches=[
                    _position_match_payload(position) for position in matches
                ],
                last_reconcile_at=last_reconcile_at,
            )
        if fresh:
            return RecoveryExposureSnapshot(
                status="absent",
                fresh=True,
                submitted_tickets=submitted_tickets,
                last_reconcile_at=last_reconcile_at,
            )
        return RecoveryExposureSnapshot(
            status="pending",
            fresh=False,
            submitted_tickets=submitted_tickets,
            pending_submitted_tickets=submitted_tickets,
            last_reconcile_at=last_reconcile_at,
        )


def submitted_ticket_items(cycle: RecoveryCycleState) -> list[dict[str, Any]]:
    return _submitted_tickets(cycle)


def matching_recovery_positions(
    cycle: RecoveryCycleState,
    positions: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    return _matching_recovery_positions(cycle, positions)


def position_ticket(position: Mapping[str, Any]) -> int | None:
    return _position_ticket(position)


def parse_reconcile_time(value: Any) -> datetime | None:
    return _parse_datetime(value)


def _submitted_tickets(cycle: RecoveryCycleState) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in list((cycle.metadata or {}).get("submitted_tickets") or []):
        if not isinstance(item, Mapping):
            continue
        try:
            ticket = int(item.get("ticket"))
        except (TypeError, ValueError):
            continue
        if ticket <= 0 or ticket in seen:
            continue
        seen.add(ticket)
        items.append(
            {
                "scope": str(item.get("scope") or f"ticket_{ticket}"),
                "step_index": int(item.get("step_index") or 0),
                "ticket": ticket,
            }
        )
    return items


def _matching_recovery_positions(
    cycle: RecoveryCycleState,
    positions: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    submitted = {int(item["ticket"]) for item in _submitted_tickets(cycle)}
    matches: list[Mapping[str, Any]] = []
    for position in positions:
        account_key = str(position.get("account_key") or "").strip()
        if account_key and account_key != cycle.account_key:
            continue
        ticket = _position_ticket(position)
        if ticket is not None and ticket in submitted:
            matches.append(position)
            continue
        if _position_identity_matches_cycle(cycle, position):
            matches.append(position)
    return matches


def _position_identity_matches_cycle(
    cycle: RecoveryCycleState,
    position: Mapping[str, Any],
) -> bool:
    cycle_id = str(cycle.cycle_id or "").strip()
    source_signal_id = str(cycle.source_signal_id or "").strip()
    metadata = position.get("metadata")
    if isinstance(metadata, Mapping):
        metadata_cycle_id = str(metadata.get("recovery_cycle_id") or "").strip()
        metadata_source_signal_id = str(metadata.get("source_signal_id") or "").strip()
        if cycle_id and metadata_cycle_id == cycle_id:
            return True
        if source_signal_id and metadata_source_signal_id == source_signal_id:
            return True

    for key in (
        "signal_id",
        "source_signal_id",
        "trace_id",
        "request_id",
        "action_id",
        "comment",
    ):
        value = str(position.get(key) or "").strip()
        if not value:
            continue
        if source_signal_id and value == source_signal_id:
            return True
        if cycle_id and cycle_id in value:
            return True
    return False


def _position_match_payload(position: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ticket": _position_ticket(position),
        "signal_id": str(position.get("signal_id") or ""),
        "comment": str(position.get("comment") or ""),
        "symbol": str(position.get("symbol") or ""),
        "strategy": str(position.get("strategy") or ""),
        "timeframe": str(position.get("timeframe") or ""),
    }


def _unique_positive_ints(values: Any) -> list[int]:
    items: list[int] = []
    seen: set[int] = set()
    for value in list(values or []):
        try:
            item = int(value)
        except (TypeError, ValueError):
            continue
        if item <= 0 or item in seen:
            continue
        seen.add(item)
        items.append(item)
    return items


def _position_ticket(position: Mapping[str, Any]) -> int | None:
    for key in ("ticket", "position_ticket", "position", "order_ticket"):
        try:
            ticket = int(position.get(key))
        except (TypeError, ValueError):
            continue
        if ticket > 0:
            return ticket
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


def _datetime_after_or_equal(left: datetime, right: datetime) -> bool:
    left_aware = left if left.tzinfo is not None else left.replace(tzinfo=timezone.utc)
    right_aware = (
        right if right.tzinfo is not None else right.replace(tzinfo=timezone.utc)
    )
    return left_aware >= right_aware


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()
