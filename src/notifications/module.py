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
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from src.config.models.notifications import NotificationConfig
from src.monitoring.pipeline.event_bus import PipelineEvent, PipelineEventBus
from src.notifications.classifier import PipelineEventClassifier
from src.notifications.dispatcher import NotificationDispatcher
from src.notifications.events import NotificationEvent
from src.notifications.hooks import (
    make_health_alert_listener,
    make_pipeline_listener,
    make_trading_state_poll_job,
)
from src.notifications.persistence.outbox import OutboxStore
from src.notifications.scheduler import NotificationScheduler
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
        trading_state_alerts: Optional[Any] = None,
        runtime_read_model: Optional[Any] = None,
        trading_state_poll_interval_seconds: float = 60.0,
        outbox_purge_interval_hours: float = 6.0,
        outbox_retention_days: float = 7.0,
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
        self._trading_state_alerts = trading_state_alerts
        self._runtime_read_model = runtime_read_model
        self._trading_state_poll_interval = float(trading_state_poll_interval_seconds)
        self._outbox_purge_interval_hours = float(outbox_purge_interval_hours)
        self._outbox_retention_days = float(outbox_retention_days)
        self._scheduler: Optional[NotificationScheduler] = None
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
            self._start_scheduler()
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
            self._stop_scheduler()
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
        scheduler_running = False
        scheduler_jobs: list[str] = []
        if self._scheduler is not None:
            scheduler_running = self._scheduler.is_running()
            scheduler_jobs = self._scheduler.job_names()
        return {
            "enabled": self._runtime_enabled_override,
            "started": self._started,
            "instance": self._instance,
            "pipeline_listener_registered": self._pipeline_listener is not None,
            "health_listener_registered": self._health_listener is not None,
            "scheduler_running": scheduler_running,
            "scheduler_jobs": scheduler_jobs,
            "dispatcher": self._dispatcher.status(),
        }

    # ── public observability: DLQ inspection ──

    def dlq_entries(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return most recent DLQ rows as plain dicts for API serialization.

        Intentionally a pass-through around ``OutboxStore.dlq_entries``; we
        convert the dataclass into a shallow dict so the admin route doesn't
        reach into ``OutboxEntry`` internals (ADR-006 port boundary).
        """
        entries = self._outbox.dlq_entries(limit=limit)
        return [
            {
                "id": e.id,
                "event_id": e.event_id,
                "event_type": e.event_type,
                "severity": e.severity,
                "chat_id": e.chat_id,
                "dedup_key": e.dedup_key,
                "created_at": e.created_at.isoformat(),
                "attempt_count": e.attempt_count,
                "last_error": e.last_error,
                "rendered_text_preview": (
                    e.rendered_text[:200] + ("…" if len(e.rendered_text) > 200 else "")
                ),
            }
            for e in entries
        ]

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

    # ── internal: scheduler wiring ──

    def _start_scheduler(self) -> None:
        """Build and start the scheduler with Phase 2 periodic jobs.

        Jobs registered:
        - ``trading_state_poll`` (if ``trading_state_alerts`` is provided):
          pulls summary() every ~60s and dispatches per-code notifications.
        - ``outbox_purge``: deletes ``status=sent`` rows older than
          ``outbox_retention_days`` every ``outbox_purge_interval_hours``.
        - ``daily_report`` (if ``schedules.daily_report_utc`` is set):
          placeholder callback logs; Phase 2.5 will plug in the PnL summary.
        """
        if self._scheduler is not None:
            return
        scheduler = NotificationScheduler(tick_seconds=1.0)
        if self._trading_state_alerts is not None:
            scheduler.add_interval(
                name="trading_state_poll",
                interval_seconds=self._trading_state_poll_interval,
                job=make_trading_state_poll_job(
                    trading_state_alerts=self._trading_state_alerts,
                    dispatcher=self._dispatcher,
                    instance=self._instance,
                ),
            )
        scheduler.add_interval(
            name="outbox_purge",
            interval_seconds=self._outbox_purge_interval_hours * 3600.0,
            job=self._make_outbox_purge_job(),
        )
        daily_spec = self._config.schedules.daily_report_utc
        if daily_spec:
            try:
                hour_str, minute_str = daily_spec.split(":")
                scheduler.add_daily(
                    name="daily_report",
                    hour_utc=int(hour_str),
                    minute_utc=int(minute_str),
                    job=self._make_daily_report_job(),
                )
            except Exception:
                logger.exception(
                    "invalid daily_report_utc=%r; scheduler job not registered",
                    daily_spec,
                )
        try:
            scheduler.start()
        except Exception:
            logger.exception("notification scheduler failed to start")
            return
        self._scheduler = scheduler

    def _stop_scheduler(self) -> None:
        if self._scheduler is None:
            return
        try:
            self._scheduler.stop()
        except Exception:
            logger.debug("scheduler stop failed", exc_info=True)
        self._scheduler = None

    def _make_outbox_purge_job(self) -> Callable[[], None]:
        outbox = self._outbox
        retention = timedelta(days=self._outbox_retention_days)

        def _purge() -> None:
            cutoff = datetime.now(timezone.utc) - retention
            try:
                deleted = outbox.purge_sent_older_than(cutoff)
                if deleted > 0:
                    logger.info(
                        "notification outbox purged %d sent rows older than %s",
                        deleted,
                        cutoff.isoformat(),
                    )
            except Exception:
                logger.exception("outbox purge failed")

        return _purge

    def _make_daily_report_job(self) -> Callable[[], None]:
        """Build the daily_report scheduler job.

        If ``runtime_read_model`` was injected, generate a real snapshot.
        Otherwise fall back to a heartbeat log — keeps the slot observable
        (``scheduler_jobs`` shows ``daily_report``) but emits nothing.
        """
        from src.notifications.daily_report import DailyReportGenerator

        instance = self._instance
        dispatcher = self._dispatcher
        rrm = self._runtime_read_model

        if rrm is None:

            def _heartbeat() -> None:
                logger.info(
                    "daily_report scheduler tick for instance=%s — "
                    "runtime_read_model not injected, skipping send",
                    instance,
                )

            return _heartbeat

        generator = DailyReportGenerator(runtime_read_model=rrm, instance=instance)

        def _emit() -> None:
            try:
                event = generator.build_event()
            except Exception:
                logger.exception("daily_report generator raised")
                return
            try:
                dispatcher.submit(event)
            except Exception:
                logger.exception("daily_report submit failed")

        return _emit

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
