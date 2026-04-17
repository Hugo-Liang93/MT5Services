"""Tests for PipelineEventClassifier."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.config.models.notifications import NotificationConfig
from src.monitoring.pipeline.event_bus import PipelineEvent
from src.monitoring.pipeline.events import (
    PIPELINE_ADMISSION_REPORT_APPENDED,
    PIPELINE_BAR_CLOSED,
    PIPELINE_EXECUTION_BLOCKED,
    PIPELINE_EXECUTION_FAILED,
    PIPELINE_EXECUTION_SUBMITTED,
    PIPELINE_RISK_STATE_CHANGED,
    PIPELINE_UNMANAGED_POSITION_DETECTED,
)
from src.notifications.classifier import (
    NOTIF_CIRCUIT_OPEN,
    NOTIF_EOD_BLOCK,
    NOTIF_EXECUTION_FAILED,
    NOTIF_EXECUTION_SUBMITTED,
    NOTIF_RISK_REJECTION,
    NOTIF_UNMANAGED_POSITION,
    PipelineEventClassifier,
)


def _make_config(
    *,
    events: dict[str, str],
    suppress_info_on: list[str] | None = None,
    rejection_reasons: list[str] | None = None,
) -> NotificationConfig:
    return NotificationConfig(
        runtime={"enabled": False},
        events=events,
        event_filters={
            "risk_rejection_reasons": rejection_reasons or [],
            "suppress_info_on_instances": suppress_info_on or [],
        },
        dedup={},
        rate_limit={},
        schedules={},
        templates={},
        inbound={"enabled": False},
        chats={"default_chat_id": ""},
        bot_token="",
    )


def _pipeline_event(
    type_: str, payload: dict, *, symbol: str = "XAUUSD"
) -> PipelineEvent:
    return PipelineEvent(
        type=type_,
        trace_id="trace-123",
        symbol=symbol,
        timeframe="H1",
        scope="confirmed",
        ts=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
        payload=payload,
    )


class TestExecutionFailed:
    def test_maps_critical(self):
        config = _make_config(events={"execution_failed": "critical"})
        classifier = PipelineEventClassifier(config=config, instance="live-main")
        event = _pipeline_event(
            PIPELINE_EXECUTION_FAILED,
            {"strategy": "trend_h1", "direction": "long", "reason": "broker_reject"},
        )
        result = classifier.classify(event)
        assert result is not None
        assert result.event_type == NOTIF_EXECUTION_FAILED
        assert result.template_key == "critical_execution_failed"
        assert result.payload["strategy"] == "trend_h1"
        assert result.payload["symbol"] == "XAUUSD"
        assert result.payload["instance"] == "live-main"
        assert result.dedup_key == "execution_failed:XAUUSD:trend_h1"

    def test_off_returns_none(self):
        config = _make_config(events={"execution_failed": "off"})
        classifier = PipelineEventClassifier(config=config, instance="live-main")
        event = _pipeline_event(PIPELINE_EXECUTION_FAILED, {"strategy": "x"})
        assert classifier.classify(event) is None

    def test_unknown_event_ignored(self):
        config = _make_config(events={"execution_failed": "critical"})
        classifier = PipelineEventClassifier(config=config, instance="live-main")
        event = _pipeline_event(PIPELINE_BAR_CLOSED, {})
        assert classifier.classify(event) is None


class TestExecutionSubmitted:
    def test_info_on_live(self):
        config = _make_config(events={"execution_submitted": "info"})
        classifier = PipelineEventClassifier(config=config, instance="live-main")
        event = _pipeline_event(
            PIPELINE_EXECUTION_SUBMITTED,
            {
                "strategy": "trend_h1",
                "direction": "long",
                "order_kind": "market",
                "ticket": "99",
            },
        )
        result = classifier.classify(event)
        assert result is not None
        assert result.event_type == NOTIF_EXECUTION_SUBMITTED
        assert result.payload["ticket"] == "99"

    def test_info_suppressed_on_demo_instance(self):
        config = _make_config(
            events={"execution_submitted": "info"},
            suppress_info_on=["demo-main"],
        )
        classifier = PipelineEventClassifier(config=config, instance="demo-main")
        event = _pipeline_event(
            PIPELINE_EXECUTION_SUBMITTED, {"strategy": "x", "direction": "long"}
        )
        assert classifier.classify(event) is None

    def test_non_info_severity_not_suppressed_by_info_filter(self):
        config = _make_config(
            events={"execution_submitted": "warning"},
            suppress_info_on=["demo-main"],
        )
        classifier = PipelineEventClassifier(config=config, instance="demo-main")
        event = _pipeline_event(
            PIPELINE_EXECUTION_SUBMITTED, {"strategy": "x", "direction": "long"}
        )
        # Severity is warning, suppress_info only applies to info — should pass.
        assert classifier.classify(event) is not None


class TestEodBlock:
    def test_maps_when_reason_has_eod(self):
        config = _make_config(events={"eod_block": "warning"})
        classifier = PipelineEventClassifier(config=config, instance="live-main")
        event = _pipeline_event(
            PIPELINE_EXECUTION_BLOCKED, {"reason": "eod_block", "strategy": "x"}
        )
        result = classifier.classify(event)
        assert result is not None
        assert result.event_type == NOTIF_EOD_BLOCK

    def test_other_block_reason_ignored(self):
        config = _make_config(events={"eod_block": "warning"})
        classifier = PipelineEventClassifier(config=config, instance="live-main")
        event = _pipeline_event(
            PIPELINE_EXECUTION_BLOCKED,
            {"reason": "margin_guard_block", "strategy": "x"},
        )
        # EXECUTION_BLOCKED with non-eod reason → no EOD notification.
        # (The risk_rejection path handles it separately.)
        assert classifier.classify(event) is None


class TestUnmanagedPosition:
    def test_maps(self):
        config = _make_config(events={"unmanaged_position": "critical"})
        classifier = PipelineEventClassifier(config=config, instance="live-main")
        event = _pipeline_event(
            PIPELINE_UNMANAGED_POSITION_DETECTED,
            {"ticket": "555", "reason": "comment_unrecognized"},
        )
        result = classifier.classify(event)
        assert result is not None
        assert result.event_type == NOTIF_UNMANAGED_POSITION
        assert result.dedup_key == "unmanaged_position:555"


class TestCircuitOpen:
    def test_maps_on_open_transition(self):
        config = _make_config(events={"circuit_open": "critical"})
        classifier = PipelineEventClassifier(config=config, instance="live-main")
        event = _pipeline_event(
            PIPELINE_RISK_STATE_CHANGED,
            {
                "state": "circuit_open",
                "consecutive_failures": 3,
                "last_reason": "timeout",
                "auto_reset_minutes": 30,
            },
        )
        result = classifier.classify(event)
        assert result is not None
        assert result.event_type == NOTIF_CIRCUIT_OPEN

    def test_non_open_state_ignored(self):
        config = _make_config(events={"circuit_open": "critical"})
        classifier = PipelineEventClassifier(config=config, instance="live-main")
        event = _pipeline_event(
            PIPELINE_RISK_STATE_CHANGED,
            {"state": "closed", "consecutive_failures": 0},
        )
        assert classifier.classify(event) is None


class TestRiskRejection:
    def test_maps_when_reason_whitelisted(self):
        config = _make_config(
            events={"risk_rejection": "warning"},
            rejection_reasons=["margin_guard_block"],
        )
        classifier = PipelineEventClassifier(config=config, instance="live-main")
        event = _pipeline_event(
            PIPELINE_ADMISSION_REPORT_APPENDED,
            {
                "verdict": "reject",
                "reason": "margin_guard_block",
                "strategy": "trend",
                "direction": "long",
            },
        )
        result = classifier.classify(event)
        assert result is not None
        assert result.event_type == NOTIF_RISK_REJECTION

    def test_reason_not_in_whitelist_filtered(self):
        config = _make_config(
            events={"risk_rejection": "warning"},
            rejection_reasons=["margin_guard_block"],
        )
        classifier = PipelineEventClassifier(config=config, instance="live-main")
        event = _pipeline_event(
            PIPELINE_ADMISSION_REPORT_APPENDED,
            {
                "verdict": "reject",
                "reason": "reentry_cooldown",
                "strategy": "trend",
                "direction": "long",
            },
        )
        assert classifier.classify(event) is None

    def test_empty_whitelist_allows_all(self):
        config = _make_config(events={"risk_rejection": "warning"})  # no reason list
        classifier = PipelineEventClassifier(config=config, instance="live-main")
        event = _pipeline_event(
            PIPELINE_ADMISSION_REPORT_APPENDED,
            {
                "verdict": "reject",
                "reason": "reentry_cooldown",
                "strategy": "trend",
                "direction": "long",
            },
        )
        result = classifier.classify(event)
        assert result is not None

    def test_non_reject_verdict_ignored(self):
        config = _make_config(events={"risk_rejection": "warning"})
        classifier = PipelineEventClassifier(config=config, instance="live-main")
        event = _pipeline_event(
            PIPELINE_ADMISSION_REPORT_APPENDED,
            {"verdict": "approve", "strategy": "x"},
        )
        assert classifier.classify(event) is None


class TestMapperExceptionIsolation:
    def test_mapper_bug_does_not_escape(self, monkeypatch):
        from src.notifications import classifier as mod

        def raising(event):
            raise RuntimeError("bug")

        monkeypatch.setitem(
            mod._PIPELINE_MAPPERS,
            PIPELINE_EXECUTION_FAILED,
            raising,
        )
        config = _make_config(events={"execution_failed": "critical"})
        classifier = PipelineEventClassifier(config=config, instance="live-main")
        event = _pipeline_event(PIPELINE_EXECUTION_FAILED, {"strategy": "x"})
        # Must NOT raise — the listener chain would die otherwise.
        assert classifier.classify(event) is None
