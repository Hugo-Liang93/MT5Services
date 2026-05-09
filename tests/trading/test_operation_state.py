from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.risk.service import PreTradeRiskBlockedError
from src.trading.application.audit import (
    TradeCommandAuditService,
    TradeDailyStatsService,
)
from src.trading.application.control import TradeControlStateService
from src.trading.models import TradeCommandAuditRecord


def test_trade_control_state_blocks_auto_entry_when_paused() -> None:
    service = TradeControlStateService()
    service.update(auto_entry_enabled=False, reason="pause")

    with pytest.raises(PreTradeRiskBlockedError) as exc_info:
        service.enforce(
            {
                "symbol": "XAUUSD",
                "volume": 0.1,
                "side": "buy",
                "metadata": {"entry_origin": "auto"},
            }
        )

    assert exc_info.value.assessment["reason"] == "auto_entry_paused"


def test_trade_daily_stats_service_builds_summary_from_records() -> None:
    stats = TradeDailyStatsService()
    recorded_at = datetime(2026, 4, 3, 2, 0, tzinfo=timezone.utc)
    stats.update(
        TradeCommandAuditRecord(
            account_alias="live",
            command_type="execute_trade",
            status="success",
            symbol="XAUUSD",
            side="buy",
            order_kind="market",
            volume=0.1,
            recorded_at=recorded_at,
            request_payload={},
            response_payload={"ticket": 1},
        )
    )

    summary = stats.summary(
        account_alias="live",
        summary_date=recorded_at.date(),
    )

    assert summary["total"] == 1
    assert summary["success"] == 1
    assert summary["symbols"]["XAUUSD"]["total"] == 1
    assert summary["operations"]["execute_trade"]["success"] == 1


class _AuditWriter:
    def __init__(self) -> None:
        self.rows = []

    def write_trade_command_audits(self, rows):
        self.rows.extend(list(rows))


def test_trade_command_audit_service_projects_broker_ids_from_response_payload() -> (
    None
):
    writer = _AuditWriter()
    service = TradeCommandAuditService(
        writer,
        account_alias_getter=lambda: "demo",
        account_key_getter=lambda: "demo:broker:1",
    )

    service.record(
        TradeCommandAuditRecord(
            account_alias="demo",
            command_type="execute_trade",
            status="success",
            request_payload={"request_id": "sig-1"},
            response_payload={
                "request_id": "sig-1",
                "ticket": 296256614,
                "order": 296256614,
                "deal": 224540391,
            },
        )
    )

    persisted = writer.rows[0]
    assert persisted[9] == 296256614
    assert persisted[10] == 296256614
    assert persisted[11] == 224540391
