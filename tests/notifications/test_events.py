"""Unit tests for notification event envelope + helpers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.notifications.events import (
    NotificationEvent,
    Severity,
    build_dedup_key,
    severity_from_str,
)


class TestSeverity:
    def test_from_string_normal(self):
        assert severity_from_str("critical") is Severity.CRITICAL
        assert severity_from_str("WARNING") is Severity.WARNING
        assert severity_from_str("  info  ") is Severity.INFO

    def test_from_severity_passthrough(self):
        assert severity_from_str(Severity.CRITICAL) is Severity.CRITICAL

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            severity_from_str("emergency")

    def test_off_rejected(self):
        # Severity enum is only for "active" events. "off" is a config-level
        # decision, not a shipped severity.
        with pytest.raises(ValueError):
            severity_from_str("off")

    def test_tag(self):
        assert Severity.CRITICAL.tag == "CRITICAL"
        assert Severity.WARNING.tag == "WARNING"


class TestDedupKey:
    def test_empty_event_type_returns_empty(self):
        assert build_dedup_key("") == ""

    def test_concatenates_parts(self):
        assert build_dedup_key("execution_failed", "XAUUSD", "breakout") == (
            "execution_failed:XAUUSD:breakout"
        )

    def test_drops_blank_parts(self):
        assert build_dedup_key("circuit_open", "", None) == "circuit_open"  # type: ignore[arg-type]


class TestNotificationEvent:
    def _valid_kwargs(self, **overrides):
        payload = dict(
            event_type="execution_failed",
            severity=Severity.CRITICAL,
            template_key="critical_execution_failed",
            source="pipeline_bus",
            instance="live-main",
            dedup_key="execution_failed:XAUUSD",
            payload={"symbol": "XAUUSD"},
        )
        payload.update(overrides)
        return payload

    def test_construct_ok(self):
        event = NotificationEvent(**self._valid_kwargs())
        assert event.event_type == "execution_failed"
        assert event.severity is Severity.CRITICAL
        assert event.ts.tzinfo is timezone.utc
        assert event.event_id  # uuid auto

    def test_missing_event_type(self):
        with pytest.raises(ValueError):
            NotificationEvent(**self._valid_kwargs(event_type=""))

    def test_missing_template_key(self):
        with pytest.raises(ValueError):
            NotificationEvent(**self._valid_kwargs(template_key=""))

    def test_missing_source(self):
        with pytest.raises(ValueError):
            NotificationEvent(**self._valid_kwargs(source=""))

    def test_severity_must_be_enum(self):
        with pytest.raises(TypeError):
            NotificationEvent(**self._valid_kwargs(severity="critical"))  # type: ignore[arg-type]

    def test_naive_ts_upgraded_to_utc(self):
        naive = datetime(2026, 1, 1, 12, 0, 0)
        event = NotificationEvent(**self._valid_kwargs(ts=naive))
        assert event.ts.tzinfo is timezone.utc

    def test_build_factory_computes_dedup_key(self):
        event = NotificationEvent.build(
            event_type="risk_rejection",
            severity="warning",
            template_key="warn_risk_rejection",
            source="pipeline_bus",
            instance="live-main",
            payload={"symbol": "XAUUSD"},
            dedup_parts=("XAUUSD", "trend_h1"),
        )
        assert event.severity is Severity.WARNING
        assert event.dedup_key == "risk_rejection:XAUUSD:trend_h1"

    def test_build_requires_non_empty_dedup_key(self):
        with pytest.raises(ValueError):
            NotificationEvent.build(
                event_type="risk_rejection",
                severity="warning",
                template_key="x",
                source="s",
                instance="i",
                dedup_parts=(),
            )

    def test_with_payload_non_destructive(self):
        event = NotificationEvent(**self._valid_kwargs())
        extended = event.with_payload({"direction": "long"})
        assert "direction" not in event.payload
        assert extended.payload["direction"] == "long"
        assert extended.event_id == event.event_id  # preserves identity
