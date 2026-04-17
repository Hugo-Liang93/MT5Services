"""Notifications phase builder.

Runs after monitoring so we have a live ``health_monitor`` and
``pipeline_event_bus`` to hook into. Runs before ``studio`` to ensure
/v1/admin/notifications/status can be populated from the first health tick.
"""

from __future__ import annotations

import logging

from src.app_runtime.container import AppContainer
from src.app_runtime.factories import create_notification_module
from src.config import get_notification_config, get_runtime_data_path

logger = logging.getLogger(__name__)

_OUTBOX_DB_FILENAME = "notifications_outbox.db"


def build_notifications_layer(container: AppContainer) -> None:
    """Construct the NotificationModule if configured.

    Silently leaves ``container.notification_module = None`` if:
    - ``notifications.ini`` declares ``enabled=false`` without credentials, OR
    - an instance without ``bot_token``/``default_chat_id`` is running.

    Errors during construction are logged and swallowed — the rest of the
    app must be able to start without Telegram connectivity.
    """
    try:
        config = get_notification_config()
    except Exception:
        logger.exception("notifications config failed to load; skipping module")
        return

    identity = container.runtime_identity
    instance = getattr(identity, "instance_name", "") or "unknown"

    try:
        outbox_path = get_runtime_data_path(_OUTBOX_DB_FILENAME)
    except Exception:
        logger.exception("failed to resolve notifications outbox path; skipping")
        return

    try:
        module = create_notification_module(
            config=config,
            instance=instance,
            outbox_path=outbox_path,
            pipeline_event_bus=container.pipeline_event_bus,
            health_monitor=container.health_monitor,
            trading_state_alerts=container.trading_state_alerts,
            runtime_read_model=container.runtime_read_model,
        )
    except Exception:
        logger.exception("failed to build notification module; continuing without it")
        return

    container.notification_module = module
    if module is not None:
        logger.info(
            "notification module built (enabled=%s, instance=%s)",
            config.runtime.enabled,
            instance,
        )
