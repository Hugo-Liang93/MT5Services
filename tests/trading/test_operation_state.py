from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.risk.service import PreTradeRiskBlockedError
from src.trading.control_state import TradeControlStateService
from src.trading.models import TradeOperationRecord
from src.trading.operation_state import TradeDailyStatsService


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
        TradeOperationRecord(
            account_alias="live",
            operation_type="execute_trade",
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
