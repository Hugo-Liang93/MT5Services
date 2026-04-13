from __future__ import annotations

from datetime import datetime, timezone

from src.persistence.repositories.trade_repo import TradeCommandAuditRepository
from src.trading.models import TradeCommandAuditRecord


class _DummyWriter:
    def __init__(self) -> None:
        self.batch_sql = None
        self.batch_rows = None

    def _json(self, payload):
        return {"wrapped": payload}

    def _batch(self, sql, rows, page_size: int = 200) -> None:
        self.batch_sql = sql
        self.batch_rows = list(rows)


def test_write_trade_command_audits_maps_account_key_before_command_type() -> None:
    writer = _DummyWriter()
    repo = TradeCommandAuditRepository(writer)
    row = TradeCommandAuditRecord(
        recorded_at=datetime(2026, 4, 12, 15, 0, tzinfo=timezone.utc),
        operation_id="op_1",
        account_alias="live_main",
        account_key="live:broker-live:1001",
        command_type="update_trade_control",
        status="success",
        request_payload={"idempotency_key": "idem_1"},
        response_payload={"action_id": "act_1"},
    ).to_row()

    repo.write_trade_command_audits([row])

    assert writer.batch_rows is not None
    assert len(writer.batch_rows) == 1
    persisted = writer.batch_rows[0]
    assert persisted[0] == row[0]
    assert persisted[1] == "op_1"
    assert persisted[2] == "live_main"
    assert persisted[3] == "live:broker-live:1001"
    assert persisted[4] == "update_trade_control"
    assert persisted[16] == {"wrapped": {"idempotency_key": "idem_1"}}
    assert persisted[17] == {"wrapped": {"action_id": "act_1"}}
