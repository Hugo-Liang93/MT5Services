from __future__ import annotations

from datetime import datetime, timezone

from src.trading.recovery.exposure_ledger import RecoveryExposureLedger
from src.trading.recovery.models import RecoveryCycleState


def _now() -> datetime:
    return datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)


def _cycle(**overrides) -> RecoveryCycleState:
    values = {
        "cycle_id": "cycle-ledger",
        "account_key": "demo:broker:1001",
        "symbol": "XAUUSD",
        "direction": "buy",
        "status": "open",
        "base_volume": 0.01,
        "total_volume": 0.01,
        "step_count": 0,
        "average_entry_price": 100.0,
        "last_entry_price": 100.0,
        "started_at": _now(),
        "updated_at": _now(),
        "last_step_at": _now(),
        "strategy": "tick_martingale_probe",
        "timeframe": "TICK",
        "source_signal_id": "cycle-ledger-signal",
        "metadata": {
            "submitted_tickets": [
                {"scope": "initial", "step_index": 0, "ticket": 7001}
            ]
        },
    }
    values.update(overrides)
    return RecoveryCycleState(**values)


def test_ledger_keeps_submitted_ticket_pending_when_snapshot_is_missing():
    ledger = RecoveryExposureLedger()

    snapshot = ledger.classify(
        _cycle(),
        positions=None,
        last_reconcile_at=None,
    )

    assert snapshot.status == "pending"
    assert snapshot.submitted_tickets == [7001]
    assert snapshot.live_position_tickets == []
    assert snapshot.absent_confirmed is False
    assert snapshot.to_payload()["status"] == "pending"


def test_ledger_keeps_submitted_ticket_pending_when_snapshot_is_stale():
    ledger = RecoveryExposureLedger()

    snapshot = ledger.classify(
        _cycle(updated_at=_now()),
        positions=[],
        last_reconcile_at=datetime(2026, 5, 8, 11, 59, 59, tzinfo=timezone.utc),
    )

    assert snapshot.status == "pending"
    assert snapshot.fresh is False
    assert snapshot.absent_confirmed is False
    assert snapshot.pending_submitted_tickets == [7001]


def test_ledger_confirms_live_exposure_from_matching_ticket():
    ledger = RecoveryExposureLedger()

    snapshot = ledger.classify(
        _cycle(),
        positions=[
            {
                "ticket": 7001,
                "account_key": "demo:broker:1001",
                "symbol": "XAUUSD",
            }
        ],
        last_reconcile_at=_now(),
    )

    assert snapshot.status == "confirmed"
    assert snapshot.fresh is True
    assert snapshot.live_position_tickets == [7001]
    assert snapshot.absent_confirmed is False


def test_ledger_confirms_absent_only_with_fresh_empty_snapshot():
    ledger = RecoveryExposureLedger()

    snapshot = ledger.classify(
        _cycle(),
        positions=[],
        last_reconcile_at=_now(),
    )

    assert snapshot.status == "absent"
    assert snapshot.fresh is True
    assert snapshot.submitted_tickets == [7001]
    assert snapshot.live_position_tickets == []
    assert snapshot.absent_confirmed is True


def test_ledger_matches_netting_position_by_cycle_identity():
    ledger = RecoveryExposureLedger()

    snapshot = ledger.classify(
        _cycle(),
        positions=[
            {
                "ticket": 9001,
                "account_key": "demo:broker:1001",
                "symbol": "XAUUSD",
                "comment": "recovery-runner:cycle-ledger:s1",
            }
        ],
        last_reconcile_at=_now(),
    )

    assert snapshot.status == "confirmed"
    assert snapshot.live_position_tickets == [9001]
    assert snapshot.position_matches == [
        {
            "ticket": 9001,
            "signal_id": "",
            "comment": "recovery-runner:cycle-ledger:s1",
            "symbol": "XAUUSD",
            "strategy": "",
            "timeframe": "",
        }
    ]
