"""Tests for TemplateRegistry — loading, validation, rendering, event dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.notifications.events import NotificationEvent, Severity
from src.notifications.templates.loader import (
    TemplateNotFoundError,
    TemplateRegistry,
    TemplateValidationError,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUILTIN_TEMPLATES = PROJECT_ROOT / "config" / "notifications" / "templates"


class TestBuiltInTemplates:
    """Integration: the shipped templates under config/notifications/templates
    must pass strict validation (this is the guardrail against drift)."""

    def test_all_builtin_templates_load_strict(self):
        registry = TemplateRegistry.from_directory(BUILTIN_TEMPLATES, strict=True)
        expected = {
            "critical_execution_failed",
            "critical_circuit_open",
            "critical_queue_overflow",
            "critical_pending_missing",
            "critical_unmanaged_position",
            "warn_risk_rejection",
            "warn_health_alert",
            "warn_eod_block",
            "info_execution_submitted",
        }
        assert expected.issubset(set(registry.keys()))

    def test_metadata_extracted(self):
        registry = TemplateRegistry.from_directory(BUILTIN_TEMPLATES, strict=True)
        meta = registry.get("critical_execution_failed")
        assert meta.severity is Severity.CRITICAL
        assert meta.tag == "CRITICAL"
        assert {
            "strategy",
            "symbol",
            "direction",
            "reason",
            "instance",
            "trace_id",
        }.issubset(meta.required_vars)


class TestRegistryLoading:
    def _write(self, directory: Path, name: str, content: str) -> None:
        (directory / f"{name}.md").write_text(content, encoding="utf-8")

    def test_missing_directory_strict_raises(self, tmp_path: Path):
        with pytest.raises(TemplateValidationError):
            TemplateRegistry.from_directory(tmp_path / "nonexistent", strict=True)

    def test_missing_directory_non_strict_empty(self, tmp_path: Path):
        registry = TemplateRegistry.from_directory(tmp_path / "nope", strict=False)
        assert list(registry.keys()) == []

    def test_missing_header_fails(self, tmp_path: Path):
        self._write(tmp_path, "no_header", "body only, no header")
        with pytest.raises(TemplateValidationError):
            TemplateRegistry.from_directory(tmp_path, strict=True)

    def test_missing_required_field_fails(self, tmp_path: Path):
        self._write(
            tmp_path,
            "bad_meta",
            "<!--\nseverity: info\ntag: T\n-->\nbody {{ x }}",
        )
        with pytest.raises(TemplateValidationError):
            TemplateRegistry.from_directory(tmp_path, strict=True)

    def test_invalid_severity_fails(self, tmp_path: Path):
        self._write(
            tmp_path,
            "bad_sev",
            "<!--\nseverity: emergency\ntag: X\nrequired_vars: foo\n-->\nbody {{ foo }}",
        )
        with pytest.raises(TemplateValidationError):
            TemplateRegistry.from_directory(tmp_path, strict=True)

    def test_undeclared_variable_fails(self, tmp_path: Path):
        self._write(
            tmp_path,
            "drift",
            "<!--\nseverity: info\ntag: I\nrequired_vars: foo\n-->\nbody {{ foo }} {{ bar }}",
        )
        with pytest.raises(TemplateValidationError):
            TemplateRegistry.from_directory(tmp_path, strict=True)

    def test_ensure_keys_missing_raises(self, tmp_path: Path):
        self._write(
            tmp_path,
            "one",
            "<!--\nseverity: info\ntag: I\nrequired_vars: foo\n-->\nbody {{ foo }}",
        )
        registry = TemplateRegistry.from_directory(tmp_path, strict=True)
        with pytest.raises(TemplateValidationError):
            registry.ensure_keys(["one", "missing_key"])


class TestRendering:
    def test_render_adds_tag_prefix_by_default(self, tmp_path: Path):
        (tmp_path / "t.md").write_text(
            "<!--\nseverity: info\ntag: INFO\nrequired_vars: name\n-->\nhi {{ name }}",
            encoding="utf-8",
        )
        registry = TemplateRegistry.from_directory(tmp_path, strict=True)
        out = registry.render("t", {"name": "world"})
        assert out == "[INFO] hi world"

    def test_render_without_tag_prefix(self, tmp_path: Path):
        (tmp_path / "t.md").write_text(
            "<!--\nseverity: info\ntag: INFO\nrequired_vars: name\n-->\nhi {{ name }}",
            encoding="utf-8",
        )
        registry = TemplateRegistry.from_directory(tmp_path, strict=True)
        out = registry.render("t", {"name": "x"}, add_tag_prefix=False)
        assert out == "hi x"

    def test_render_missing_required_context_raises(self, tmp_path: Path):
        (tmp_path / "t.md").write_text(
            "<!--\nseverity: info\ntag: T\nrequired_vars: name\n-->\nhi {{ name }}",
            encoding="utf-8",
        )
        registry = TemplateRegistry.from_directory(tmp_path, strict=True)
        with pytest.raises(TemplateValidationError):
            registry.render("t", {})

    def test_get_unknown_key_raises(self, tmp_path: Path):
        registry = TemplateRegistry(directory=tmp_path, strict=False)
        with pytest.raises(TemplateNotFoundError):
            registry.get("nope")

    def test_render_event_exposes_payload_fields(self):
        registry = TemplateRegistry.from_directory(BUILTIN_TEMPLATES, strict=True)
        event = NotificationEvent.build(
            event_type="execution_failed",
            severity="critical",
            template_key="critical_execution_failed",
            source="pipeline_bus",
            instance="live-main",
            payload={
                "strategy": "trend_h1",
                "symbol": "XAUUSD",
                "direction": "long",
                "reason": "broker_reject",
                "trace_id": "abc123",
            },
            dedup_parts=("XAUUSD", "trend_h1"),
        )
        message = registry.render_event(event)
        assert "[CRITICAL]" in message
        assert "trend_h1" in message
        assert "XAUUSD" in message
        assert "abc123" in message
