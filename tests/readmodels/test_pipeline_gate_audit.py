from __future__ import annotations

from datetime import datetime, timezone

from src.readmodels.pipeline_gate_audit import PipelineGateAuditReadModel


class _PipelineTraceRepo:
    def __init__(self, rows):
        self._rows = rows
        self.last_kwargs = None

    def fetch_gate_events(self, **kwargs):
        self.last_kwargs = kwargs
        return list(self._rows)


def test_pipeline_gate_audit_groups_dynamic_and_exact_gate_reasons() -> None:
    rows = [
        {
            "trace_id": "trace-1",
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "scope": "confirmed",
            "event_type": "signal_filter_decided",
            "gate_reason": "session_transition_cooldown:london_to_new_york",
            "gate_category": "session_transition_cooldown",
            "gate_source": "signal_filter",
            "recorded_at": datetime(2026, 4, 13, 13, 16, tzinfo=timezone.utc),
            "evaluation_time": datetime(2026, 4, 13, 13, 10, tzinfo=timezone.utc),
        },
        {
            "trace_id": "trace-2",
            "symbol": "XAUUSD",
            "timeframe": "M1",
            "scope": "intrabar",
            "event_type": "execution_blocked",
            "gate_reason": "quote_stale",
            "gate_category": "market_data",
            "gate_source": "execution",
            "recorded_at": datetime(2026, 4, 13, 13, 17, tzinfo=timezone.utc),
            "evaluation_time": datetime(2026, 4, 13, 13, 17, tzinfo=timezone.utc),
        },
        {
            "trace_id": "trace-3",
            "symbol": "XAUUSD",
            "timeframe": "M15",
            "scope": "intrabar",
            "event_type": "execution_skipped",
            "gate_reason": "intrabar_synthesis_stale",
            "gate_category": "market_data",
            "gate_source": "execution",
            "recorded_at": datetime(2026, 4, 14, 1, 0, tzinfo=timezone.utc),
            "evaluation_time": datetime(2026, 4, 14, 1, 0, tzinfo=timezone.utc),
        },
    ]
    repo = _PipelineTraceRepo(rows)
    model = PipelineGateAuditReadModel(pipeline_trace_repo=repo)

    summary = model.summarize_gate_events(
        from_time=datetime(2026, 4, 13, 0, 0, tzinfo=timezone.utc),
        to_time=datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc),
        limit=10,
    )

    assert summary["period"]["event_count"] == 3
    assert summary["period"]["trace_count"] == 3
    assert summary["period"]["truncated"] is False
    assert repo.last_kwargs["limit"] == 10

    by_family = {item["gate_family"]: item for item in summary["by_family"]}
    assert by_family["session_transition_cooldown"]["events"] == 1
    assert by_family["session_transition_cooldown"]["sources"] == {"signal_filter": 1}
    assert by_family["quote_stale"]["events"] == 1
    assert by_family["intrabar_synthesis_stale"]["events"] == 1

    by_reason = {item["gate_reason"]: item for item in summary["by_reason"]}
    assert (
        by_reason["session_transition_cooldown:london_to_new_york"]["events"] == 1
    )

    by_day = {item["day"]: item for item in summary["by_day"]}
    assert by_day["2026-04-13"]["families"]["session_transition_cooldown"] == 1
    assert by_day["2026-04-13"]["families"]["quote_stale"] == 1
    assert by_day["2026-04-14"]["families"]["intrabar_synthesis_stale"] == 1


def test_pipeline_gate_audit_can_filter_by_gate_family_and_source() -> None:
    rows = [
        {
            "trace_id": "trace-a",
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "scope": "confirmed",
            "event_type": "signal_filter_decided",
            "gate_reason": "session_transition_cooldown:london_to_new_york",
            "gate_category": "session_transition_cooldown",
            "gate_source": "signal_filter",
            "recorded_at": datetime(2026, 4, 13, 13, 16, tzinfo=timezone.utc),
            "evaluation_time": datetime(2026, 4, 13, 13, 10, tzinfo=timezone.utc),
        },
        {
            "trace_id": "trace-b",
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "scope": "confirmed",
            "event_type": "execution_blocked",
            "gate_reason": "quote_stale",
            "gate_category": "market_data",
            "gate_source": "execution",
            "recorded_at": datetime(2026, 4, 13, 13, 17, tzinfo=timezone.utc),
            "evaluation_time": datetime(2026, 4, 13, 13, 17, tzinfo=timezone.utc),
        },
    ]
    repo = _PipelineTraceRepo(rows)
    model = PipelineGateAuditReadModel(pipeline_trace_repo=repo)

    summary = model.summarize_gate_events(
        from_time=datetime(2026, 4, 13, 0, 0, tzinfo=timezone.utc),
        to_time=datetime(2026, 4, 14, 0, 0, tzinfo=timezone.utc),
        gate_families=["quote_stale"],
        sources=["execution"],
        limit=1,
    )

    assert summary["period"]["event_count"] == 1
    assert summary["period"]["trace_count"] == 1
    assert summary["period"]["truncated"] is True
    assert summary["by_family"][0]["gate_family"] == "quote_stale"
    assert summary["by_source"] == [
        {"gate_source": "execution", "events": 1, "trace_count": 1}
    ]

