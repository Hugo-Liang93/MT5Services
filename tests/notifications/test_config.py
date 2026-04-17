"""Tests for NotificationConfig Pydantic model + get_notification_config loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config.models.notifications import NotificationConfig


def _base_kwargs(**overrides):
    payload = dict(
        runtime={"enabled": False},
        events={},
        event_filters={},
        dedup={},
        rate_limit={},
        schedules={},
        templates={},
        inbound={"enabled": False},
        chats={"default_chat_id": ""},
        bot_token="",
    )
    payload.update(overrides)
    return payload


class TestNotificationConfigValidation:
    def test_defaults_disabled_no_credentials(self):
        config = NotificationConfig(**_base_kwargs())
        assert config.runtime.enabled is False
        assert config.bot_token.get_secret_value() == ""

    def test_enabled_requires_bot_token(self):
        with pytest.raises(ValueError, match="bot_token"):
            NotificationConfig(
                **_base_kwargs(
                    runtime={"enabled": True},
                    chats={"default_chat_id": "123"},
                    bot_token="",
                )
            )

    def test_enabled_requires_default_chat_id(self):
        with pytest.raises(ValueError, match="default_chat_id"):
            NotificationConfig(
                **_base_kwargs(
                    runtime={"enabled": True},
                    bot_token="xxx",
                    chats={"default_chat_id": ""},
                )
            )

    def test_enabled_ok_with_credentials(self):
        config = NotificationConfig(
            **_base_kwargs(
                runtime={"enabled": True},
                bot_token="xxx",
                chats={"default_chat_id": "123"},
            )
        )
        assert config.runtime.enabled is True

    def test_poll_interval_must_be_positive(self):
        with pytest.raises(ValueError):
            NotificationConfig(
                **_base_kwargs(runtime={"enabled": False, "poll_interval_seconds": 0})
            )

    def test_retry_backoff_cannot_be_empty(self):
        with pytest.raises(ValueError):
            NotificationConfig(
                **_base_kwargs(runtime={"enabled": False, "retry_backoff_seconds": []})
            )

    def test_retry_backoff_must_be_positive(self):
        with pytest.raises(ValueError):
            NotificationConfig(
                **_base_kwargs(
                    runtime={"enabled": False, "retry_backoff_seconds": [1, -2, 3]}
                )
            )

    def test_inbound_admin_must_be_subset(self):
        with pytest.raises(ValueError, match="subset"):
            NotificationConfig(
                **_base_kwargs(
                    inbound={
                        "enabled": True,
                        "allowed_chat_ids": ["100"],
                        "admin_chat_ids": ["999"],
                    }
                )
            )

    def test_inbound_enabled_needs_allowed_ids(self):
        with pytest.raises(ValueError, match="allowed_chat_ids"):
            NotificationConfig(
                **_base_kwargs(inbound={"enabled": True, "allowed_chat_ids": []})
            )

    def test_daily_report_utc_format(self):
        config = NotificationConfig(
            **_base_kwargs(schedules={"daily_report_utc": "9:5"})
        )
        assert config.schedules.daily_report_utc == "09:05"

    def test_daily_report_utc_invalid(self):
        with pytest.raises(ValueError):
            NotificationConfig(**_base_kwargs(schedules={"daily_report_utc": "25:00"}))
        with pytest.raises(ValueError):
            NotificationConfig(**_base_kwargs(schedules={"daily_report_utc": "abc"}))


class TestConfigHelpers:
    def test_is_event_active(self):
        config = NotificationConfig(
            **_base_kwargs(events={"a": "critical", "b": "off"})
        )
        assert config.is_event_active("a") is True
        assert config.is_event_active("b") is False
        assert config.is_event_active("unknown") is False

    def test_severity_for(self):
        config = NotificationConfig(**_base_kwargs(events={"a": "warning"}))
        assert config.severity_for("a") == "warning"
        assert config.severity_for("missing") == "off"

    def test_dedup_ttl(self):
        config = NotificationConfig(
            **_base_kwargs(
                dedup={
                    "critical_ttl_seconds": 10,
                    "warning_ttl_seconds": 20,
                    "info_ttl_seconds": 30,
                }
            )
        )
        assert config.dedup_ttl("critical") == 10
        assert config.dedup_ttl("warning") == 20
        assert config.dedup_ttl("info") == 30
        assert config.dedup_ttl("off") == 0  # type: ignore[arg-type]


class TestLoader:
    """Integration: ensure bundled config/notifications.ini parses cleanly."""

    def test_loader_reads_bundled_ini(self, monkeypatch, tmp_path):
        from src.config import notifications as notif_loader

        notif_loader.get_notification_config.cache_clear()
        config = notif_loader.get_notification_config()
        # Event catalog + schedule come from the shared base ini, which is NOT
        # overridable by *.local.ini at the [events] level (they're set there
        # explicitly to `off`/severity values). ``runtime.enabled`` is
        # intentionally omitted because user local.ini may legitimately
        # toggle it on for real deployment — the catalog invariants are
        # what we lock in here.
        assert config.events.get("execution_failed") == "critical"
        # Events without real source hooks default to `off` to avoid drift
        # (daily_report waits on Phase 2.5 content generator).
        assert config.events.get("daily_report") == "off"
        assert config.events.get("mode_changed") == "off"
        # execution_submitted is "off" by default to avoid per-order flooding.
        assert config.events.get("execution_submitted") == "off"
        assert config.schedules.daily_report_utc == "21:00"
        notif_loader.get_notification_config.cache_clear()

    def test_loader_rejects_bad_severity(self, tmp_path: Path, monkeypatch):
        from src.config import notifications as notif_loader

        # Inject a fake merged config via monkeypatching get_merged_config
        def fake_merged(name):
            assert name == "notifications.ini"
            return {"events": {"x": "emergency"}}

        monkeypatch.setattr(notif_loader, "get_merged_config", fake_merged)
        notif_loader.get_notification_config.cache_clear()
        try:
            with pytest.raises(ValueError, match="invalid severity"):
                notif_loader.get_notification_config()
        finally:
            notif_loader.get_notification_config.cache_clear()
