from __future__ import annotations

from datetime import datetime, timezone

from src.readmodels.trade_trace import TradingFlowTraceReadModel


class _SignalRepo:
    def fetch_signal_event_by_id(self, *, signal_id: str, scope: str = "confirmed"):
        if scope == "preview":
            return {
                "generated_at": datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
                "signal_id": signal_id,
                "symbol": "XAUUSD",
                "timeframe": "M15",
                "strategy": "trendline",
                "direction": "buy",
                "confidence": 0.62,
                "metadata": {"signal_trace_id": "trace-1"},
            }
        return {
            "generated_at": datetime(2026, 1, 1, 8, 15, tzinfo=timezone.utc),
            "signal_id": signal_id,
            "symbol": "XAUUSD",
            "timeframe": "M15",
            "strategy": "trendline",
            "direction": "buy",
            "confidence": 0.68,
            "metadata": {"signal_trace_id": "trace-1"},
        }

    def fetch_auto_executions(self, *, signal_id: str, limit: int = 50):
        return [
            {
                "executed_at": datetime(2026, 1, 1, 8, 16, tzinfo=timezone.utc),
                "signal_id": signal_id,
                "symbol": "XAUUSD",
                "direction": "buy",
                "strategy": "trendline",
                "success": True,
            }
        ]

    def fetch_signal_outcomes(self, *, signal_id: str, limit: int = 20):
        return []

    def fetch_trade_outcomes(self, *, signal_id: str, limit: int = 20):
        return [
            {
                "recorded_at": datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc),
                "signal_id": signal_id,
                "won": True,
                "price_change": 10.5,
            }
        ]


class _TradeRepo:
    def fetch_trace_operations(self, *, account_alias: str, signal_id: str, limit: int = 100):
        return [
            {
                "recorded_at": datetime(2026, 1, 1, 8, 16, 30, tzinfo=timezone.utc),
                "operation_id": "op-1",
                "command_type": "execute_trade",
                "status": "success",
                "request_payload": {"request_id": signal_id},
                "response_payload": {
                    "request_id": signal_id,
                    "ticket": 7001,
                },
                "ticket": 7001,
                "order_id": 7001,
                "deal_id": 9001,
            }
        ]


class _PipelineTraceRepo:
    def fetch_pipeline_trace_events(self, *, trace_ids, limit: int = 500):
        assert trace_ids == ["trace-1"]
        return [
            {
                "id": 1,
                "trace_id": "trace-1",
                "symbol": "XAUUSD",
                "timeframe": "M15",
                "scope": "confirmed",
                "event_type": "bar_closed",
                "recorded_at": datetime(2026, 1, 1, 7, 59, tzinfo=timezone.utc),
                "payload": {"bar_time": "2026-01-01T07:59:00+00:00"},
            },
            {
                "id": 2,
                "trace_id": "trace-1",
                "symbol": "XAUUSD",
                "timeframe": "M15",
                "scope": "confirmed",
                "event_type": "indicator_computed",
                "recorded_at": datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
                "payload": {"indicator_count": 5},
            },
            {
                "id": 3,
                "trace_id": "trace-1",
                "symbol": "XAUUSD",
                "timeframe": "M15",
                "scope": "confirmed",
                "event_type": "snapshot_published",
                "recorded_at": datetime(2026, 1, 1, 8, 0, 5, tzinfo=timezone.utc),
                "payload": {"indicator_count": 5},
            },
            {
                "id": 4,
                "trace_id": "trace-1",
                "symbol": "XAUUSD",
                "timeframe": "M15",
                "scope": "confirmed",
                "event_type": "signal_filter_decided",
                "recorded_at": datetime(2026, 1, 1, 8, 0, 10, tzinfo=timezone.utc),
                "payload": {"allowed": True, "category": "_pass", "reason": ""},
            },
            {
                "id": 5,
                "trace_id": "trace-1",
                "symbol": "XAUUSD",
                "timeframe": "M15",
                "scope": "confirmed",
                "event_type": "signal_evaluated",
                "recorded_at": datetime(2026, 1, 1, 8, 15, tzinfo=timezone.utc),
                "payload": {
                    "strategy": "trendline",
                    "direction": "buy",
                    "signal_state": "confirmed",
                },
            },
        ]


class _TradingStateRepo:
    def fetch_pending_order_states(self, *, account_alias=None, statuses=None, signal_id=None, limit=500):
        return [
            {
                "order_ticket": 7001,
                "signal_id": signal_id,
                "status": "filled",
                "filled_at": datetime(2026, 1, 1, 8, 17, tzinfo=timezone.utc),
                "position_ticket": 8001,
            }
        ]

    def fetch_position_runtime_states(self, *, account_alias=None, statuses=None, signal_id=None, limit=500):
        return [
            {
                "position_ticket": 8001,
                "order_ticket": 7001,
                "signal_id": signal_id,
                "status": "closed",
                "opened_at": datetime(2026, 1, 1, 8, 17, tzinfo=timezone.utc),
                "closed_at": datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc),
            }
        ]


def test_trade_trace_projection_aggregates_signal_to_outcome_chain() -> None:
    read_model = TradingFlowTraceReadModel(
        signal_repo=_SignalRepo(),
        command_audit_repo=_TradeRepo(),
        pipeline_trace_repo=_PipelineTraceRepo(),
        trading_state_repo=_TradingStateRepo(),
        account_alias_getter=lambda: "live",
    )

    trace = read_model.trace_by_signal_id("sig-1")

    assert trace["found"] is True
    assert trace["identifiers"]["signal_id"] == "sig-1"
    assert trace["identifiers"]["trace_ids"] == ["trace-1"]
    assert trace["identifiers"]["order_tickets"] == [7001]
    assert trace["identifiers"]["position_tickets"] == [8001]
    assert trace["summary"]["stages"]["pipeline_bar_closed"] == "present"
    assert trace["summary"]["stages"]["pipeline_signal_filter"] == "present"
    assert trace["summary"]["pipeline_event_counts"]["signal_filter_decided"] == 1
    assert trace["summary"]["stages"]["confirmed_signal"] == "present"
    assert trace["summary"]["pending_status_counts"]["filled"] == 1
    assert trace["summary"]["position_status_counts"]["closed"] == 1
    assert trace["summary"]["command_counts"]["execute_trade"] == 1
    assert trace["timeline"][0]["stage"] == "pipeline.bar_closed"
    assert trace["timeline"][-1]["stage"] == "outcome.trade"
    assert len(trace["graph"]["nodes"]) == len(trace["timeline"])
    assert len(trace["graph"]["edges"]) == len(trace["timeline"]) - 1
