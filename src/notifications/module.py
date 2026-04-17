"""NotificationModule — top-level composition root for the notification system.

Holds the dispatcher + transport + hooks, owns the SQLite outbox lifecycle,
and registers bus / health listeners on ``start()``. The DI builder only
calls the factory and then ``module.start() / module.stop()``.

Responsibilities deliberately NOT in this module:
- Constructing individual components (that's the factory's job).
- Knowing which events exist (the classifier + config own that).
- Any HTTP handling (that's the transport's / inbound poller's job).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from src.config.models.notifications import NotificationConfig
from src.monitoring.pipeline.event_bus import PipelineEvent, PipelineEventBus
from src.notifications.classifier import PipelineEventClassifier
from src.notifications.dispatcher import NotificationDispatcher
from src.notifications.events import NotificationEvent
from src.notifications.hooks import make_health_alert_listener, make_pipeline_listener
from src.notifications.persistence.outbox import OutboxStore
from src.notifications.templates.loader import TemplateRegistry
from src.notifications.transport.base import NotificationTransport

logger = logging.getLogger(__name__)


class NotificationModule:
    """Top-level notification component owned by the AppContainer."""

    def __init__(
        self,
        *,
        config: NotificationConfig,
        instance: str,
        dispatcher: NotificationDispatcher,
        classifier: PipelineEventClassifier,
        templates: TemplateRegistry,
        transport: NotificationTransport,
        outbox: OutboxStore,
        pipeline_event_bus: Optional[PipelineEventBus] = None,
        health_monitor: Optional[Any] = None,
    ) -> None:
        self._config = config
        self._instance = instance
        self._dispatcher = dispatcher
        self._classifier = classifier
        self._templates = templates
        self._transport = transport
        self._outbox = outbox
        self._pipeline_event_bus = pipeline_event_bus
        self._health_monitor = health_monitor
        self._pipeline_listener: Optional[Callable[[PipelineEvent], None]] = None
        self._health_listener: Optional[Callable[..., None]] = None
        self._started = False
        # Local override: lets /v1/admin/notifications/toggle turn the module
        # off without re-editing config ini. Defaults to whatever the loaded
        # config says.
        self._runtime_enabled_override = bool(config.runtime.enabled)
        self._lock = threading.Lock()

    # ── public lifecycle ──

    def start(self) -> None:
        with self._lock:
            if self._started:
                logger.debug("notification module already started")
                return
            if not self._runtime_enabled_override:
                logger.info(
                    "notification module start() ignored — runtime.enabled=false"
                )
                return
            self._register_pipeline_listener()
            self._register_health_listener()
            self._dispatcher.start()
            self._started = True
            logger.info(
                "notification module started (instance=%s, inbound_enabled=%s)",
                self._instance,
                self._config.runtime.inbound_enabled,
            )

    def stop(self) -> None:
        """Stop the worker and unsubscribe listeners.

        The outbox is NOT closed here — operators can still query /status
        (which reads outbox counts) after a toggle-off, and a subsequent
        toggle-on must be able to resume delivery of any pending rows.
        Call :meth:`close` for terminal teardown.
        """
        with self._lock:
            if not self._started:
                return
            self._unregister_health_listener()
            self._unregister_pipeline_listener()
            try:
                self._dispatcher.stop()
            except Exception:
                logger.debug("dispatcher stop failed", exc_info=True)
            self._started = False
            logger.info("notification module stopped")

    def close(self) -> None:
        """Terminal teardown: stop + release outbox DB handle."""
        self.stop()
        try:
            self._outbox.close()
        except Exception:
            logger.debug("outbox close failed", exc_info=True)

    # ── public observability ──

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._runtime_enabled_override,
            "started": self._started,
            "instance": self._instance,
            "pipeline_listener_registered": self._pipeline_listener is not None,
            "health_listener_registered": self._health_listener is not None,
            "dispatcher": self._dispatcher.status(),
        }

    # ── public submission path (used by scheduler, tests) ──

    def submit(
        self, event: NotificationEvent, *, chat_id: Optional[str] = None
    ) -> bool:
        """Direct submit bypass for callers that construct NotificationEvent themselves."""
        return self._dispatcher.submit(event, chat_id=chat_id)

    # ── internal: listener wiring ──

    def _register_pipeline_listener(self) -> None:
        if self._pipeline_event_bus is None:
            return
        listener = make_pipeline_listener(
            classifier=self._classifier, dispatcher=self._dispatcher
        )
        try:
            self._pipeline_event_bus.add_listener(listener)
        except Exception:
            logger.exception("failed to register pipeline listener")
            return
        self._pipeline_listener = listener

    def _unregister_pipeline_listener(self) -> None:
        if self._pipeline_listener is None or self._pipeline_event_bus is None:
            return
        remover = getattr(self._pipeline_event_bus, "remove_listener", None)
        if callable(remover):
            try:
                remover(self._pipeline_listener)
            except Exception:
                logger.debug("pipeline remove_listener failed", exc_info=True)
        self._pipeline_listener = None

    def _register_health_listener(self) -> None:
        if self._health_monitor is None:
            return
        setter = getattr(self._health_monitor, "set_alert_listener", None)
        if not callable(setter):
            logger.debug("health_monitor has no set_alert_listener; skipping hook")
            return
        listener = make_health_alert_listener(
            dispatcher=self._dispatcher, instance=self._instance
        )
        try:
            setter(listener)
        except Exception:
            logger.exception("failed to install health alert listener")
            return
        self._health_listener = listener

    def _unregister_health_listener(self) -> None:
        if self._health_listener is None or self._health_monitor is None:
            return
        setter = getattr(self._health_monitor, "set_alert_listener", None)
        if callable(setter):
            try:
                setter(None)
            except Exception:
                logger.debug("health set_alert_listener(None) failed", exc_info=True)
        self._health_listener = None

    # ── runtime toggle (used by /v1/admin/notifications/toggle) ──

    def set_enabled(self, enabled: bool) -> None:
        """Enable / disable submission + worker at runtime without restart.

        Disabling: stops the worker and unregisters listeners. Does NOT
        re-edit the config ini file — the override lasts until process
        restart. Re-enabling requires the original config to have been built
        with valid credentials (bot_token + default_chat_id); otherwise the
        factory would have refused to build this module in the first place.
        """
        want_enabled = bool(enabled)
        with self._lock:
            self._runtime_enabled_override = want_enabled
        if want_enabled:
            if not self._started:
                self.start()
        else:
            if self._started:
                self.stop()
