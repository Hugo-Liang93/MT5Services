"""Factory for the Telegram notification module.

Encapsulates the decision of *whether* to build the module at all (disabled
config / missing credentials → ``None``) and the wiring order of the
components inside it. The DI builder sees a single function with no concern
for the module's internals.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from src.config.models.notifications import NotificationConfig
from src.monitoring.pipeline.event_bus import PipelineEventBus
from src.notifications.classifier import PipelineEventClassifier
from src.notifications.dispatcher import NotificationDispatcher
from src.notifications.module import NotificationModule
from src.notifications.persistence.outbox import OutboxStore
from src.notifications.templates.loader import TemplateRegistry
from src.notifications.transport.base import NotificationTransport
from src.notifications.transport.telegram import TelegramTransport

logger = logging.getLogger(__name__)


def create_notification_module(
    *,
    config: NotificationConfig,
    instance: str,
    outbox_path: str | Path,
    pipeline_event_bus: Optional[PipelineEventBus] = None,
    health_monitor: Optional[Any] = None,
    trading_state_alerts: Optional[Any] = None,
    runtime_read_model: Optional[Any] = None,
    transport: Optional[NotificationTransport] = None,
    templates_dir: Optional[str | Path] = None,
) -> Optional[NotificationModule]:
    """Build a NotificationModule, or return ``None`` if the safe-default
    conditions for building it are not met.

    Returns ``None`` when:
    - ``config.runtime.enabled`` is false AND no ``bot_token`` is configured
      (nothing to build — keep the container slot empty).
    - ``config.chats.default_chat_id`` is missing.

    When ``enabled=false`` but credentials ARE set, the module IS built (so
    /toggle can flip it on without a restart), just not started.
    """
    if not instance:
        raise ValueError("instance required")
    bot_token_value = config.bot_token.get_secret_value().strip()
    default_chat = config.chats.default_chat_id

    # Hard refuse: no credentials means we cannot ever deliver, so don't
    # construct the transport (which would also raise on empty token).
    if not bot_token_value or not default_chat:
        if config.runtime.enabled:
            # Should have been caught by Pydantic model_validator; defensive.
            logger.warning(
                "notifications: enabled=true but credentials missing — skipping"
            )
        else:
            logger.info(
                "notifications: no bot_token/chat_id configured — module not built"
            )
        return None

    templates = TemplateRegistry.from_directory(
        templates_dir or config.templates.directory,
        strict=config.templates.strict_validation,
    )
    # Boot-time sanity: every event set to non-``off`` must have a template
    # under the declared template_key. Fail loud here rather than at first fire.
    required_keys: list[str] = []
    from src.notifications.classifier import _TEMPLATE_KEY as CLASSIFIER_TEMPLATE_KEYS

    for event_type, severity in config.events.items():
        if severity == "off":
            continue
        key = CLASSIFIER_TEMPLATE_KEYS.get(event_type)
        if key:
            required_keys.append(key)
    if required_keys:
        templates.ensure_keys(required_keys)

    if transport is None:
        transport = TelegramTransport(
            bot_token=config.bot_token,
            timeout_seconds=config.runtime.http_timeout_seconds,
            proxy_url=config.runtime.http_proxy_url,
        )

    outbox = OutboxStore(outbox_path)

    classifier = PipelineEventClassifier(config=config, instance=instance)

    dispatcher = NotificationDispatcher(
        config=config,
        templates=templates,
        transport=transport,
        outbox=outbox,
        default_chat_id=default_chat,
    )

    return NotificationModule(
        config=config,
        instance=instance,
        dispatcher=dispatcher,
        classifier=classifier,
        templates=templates,
        transport=transport,
        outbox=outbox,
        pipeline_event_bus=pipeline_event_bus,
        health_monitor=health_monitor,
        trading_state_alerts=trading_state_alerts,
        runtime_read_model=runtime_read_model,
    )
