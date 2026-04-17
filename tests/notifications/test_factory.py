"""Tests for the notification module factory wiring decisions."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.app_runtime.factories.notifications import create_notification_module
from src.config.models.notifications import NotificationConfig
from src.notifications.transport.base import NotificationTransport, TransportResult


class _NoopTransport(NotificationTransport):
    def send(self, *, chat_id: str, text: str) -> TransportResult:
        return TransportResult(ok=True)


def _config(**overrides) -> NotificationConfig:
    payload = dict(
        runtime={"enabled": True},
        events={"execution_failed": "critical"},
        event_filters={},
        dedup={},
        rate_limit={},
        schedules={},
        templates={},
        inbound={"enabled": False},
        chats={"default_chat_id": "123"},
        bot_token="fake-token",
    )
    payload.update(overrides)
    return NotificationConfig(**payload)


class TestRefusals:
    def test_missing_token_returns_none(self, tmp_path: Path):
        cfg = _config(
            runtime={"enabled": False},  # enabled=false allows empty token
            bot_token="",
            chats={"default_chat_id": "123"},
        )
        module = create_notification_module(
            config=cfg,
            instance="live",
            outbox_path=tmp_path / "outbox.db",
            transport=_NoopTransport(),
        )
        assert module is None

    def test_missing_chat_returns_none(self, tmp_path: Path):
        cfg = _config(
            runtime={"enabled": False},
            bot_token="token",
            chats={"default_chat_id": ""},
        )
        module = create_notification_module(
            config=cfg,
            instance="live",
            outbox_path=tmp_path / "outbox.db",
            transport=_NoopTransport(),
        )
        assert module is None

    def test_empty_instance_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="instance"):
            create_notification_module(
                config=_config(),
                instance="",
                outbox_path=tmp_path / "outbox.db",
                transport=_NoopTransport(),
            )


class TestConstruction:
    def test_builds_disabled_module_when_token_present(self, tmp_path: Path):
        cfg = _config(runtime={"enabled": False})
        module = create_notification_module(
            config=cfg,
            instance="live",
            outbox_path=tmp_path / "outbox.db",
            transport=_NoopTransport(),
        )
        assert module is not None
        status = module.status()
        assert status["enabled"] is False
        assert status["started"] is False

    def test_builds_enabled_module(self, tmp_path: Path):
        cfg = _config()  # enabled=true
        module = create_notification_module(
            config=cfg,
            instance="live",
            outbox_path=tmp_path / "outbox.db",
            transport=_NoopTransport(),
        )
        assert module is not None
        status = module.status()
        assert status["enabled"] is True

    def test_template_validation_fails_on_missing_template(self, tmp_path: Path):
        # Direct the factory at an empty templates dir → must raise.
        empty_templates = tmp_path / "tpl"
        empty_templates.mkdir()
        cfg = _config()
        from src.notifications.templates.loader import TemplateValidationError

        with pytest.raises(TemplateValidationError):
            create_notification_module(
                config=cfg,
                instance="live",
                outbox_path=tmp_path / "outbox.db",
                transport=_NoopTransport(),
                templates_dir=empty_templates,
            )
