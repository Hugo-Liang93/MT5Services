"""StudioService - aggregation layer with registry pattern."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable

from .event_buffer import EventBuffer
from .models import build_agent

logger = logging.getLogger(__name__)


class StudioService:
    """Central aggregation service for the Studio observability layer."""

    def __init__(
        self,
        event_buffer_size: int = 200,
        snapshot_ttl_seconds: float = 3.0,
    ) -> None:
        self._agent_providers: dict[str, Callable[[], dict[str, Any]]] = {}
        self._summary_providers: list[Callable[[], dict[str, Any]]] = []
        self._event_buffer = EventBuffer(event_buffer_size)
        self._snapshot_ttl_seconds = max(float(snapshot_ttl_seconds), 0.0)
        self._snapshot_lock = threading.Lock()
        self._cached_agents: list[dict[str, Any]] = []
        self._cached_summary: dict[str, Any] = {}
        self._snapshot_built_at = 0.0
        self._subscribers: list[
            tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]
        ] = []
        self._sub_lock = threading.Lock()

    def register_agent(
        self,
        agent_id: str,
        provider: Callable[[], dict[str, Any]],
    ) -> None:
        self._agent_providers[agent_id] = provider
        self.invalidate_snapshot()

    def register_summary_provider(
        self,
        provider: Callable[[], dict[str, Any]],
    ) -> None:
        self._summary_providers.append(provider)
        self.invalidate_snapshot()

    def invalidate_snapshot(self) -> None:
        with self._snapshot_lock:
            self._cached_agents = []
            self._cached_summary = {}
            self._snapshot_built_at = 0.0

    def refresh_snapshot(self, *, force: bool = False) -> dict[str, Any]:
        with self._snapshot_lock:
            now = time.monotonic()
            cache_valid = (
                not force
                and self._cached_agents
                and (now - self._snapshot_built_at) < self._snapshot_ttl_seconds
            )
            if cache_valid:
                return {
                    "agents": list(self._cached_agents),
                    "summary": dict(self._cached_summary),
                }

            agents = self._build_agents_uncached()
            summary = self._build_summary_uncached(agents)
            self._cached_agents = agents
            self._cached_summary = summary
            self._snapshot_built_at = now
            return {
                "agents": list(self._cached_agents),
                "summary": dict(self._cached_summary),
            }

    def build_agents(self) -> list[dict[str, Any]]:
        snapshot = self.refresh_snapshot()
        return list(snapshot["agents"])

    def _build_agents_uncached(self) -> list[dict[str, Any]]:
        agents: list[dict[str, Any]] = []
        for agent_id, provider in self._agent_providers.items():
            try:
                agents.append(provider())
            except Exception:
                logger.debug("Agent provider %s failed", agent_id, exc_info=True)
                agents.append(
                    build_agent(agent_id, "error", "状态查询失败", alert_level="error")
                )
        return agents

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._event_buffer.recent(limit)

    def build_summary(
        self,
        agents: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if agents is None:
            snapshot = self.refresh_snapshot()
            return dict(snapshot["summary"])
        return self._build_summary_uncached(agents)

    def _build_summary_uncached(
        self,
        agents: list[dict[str, Any]],
    ) -> dict[str, Any]:
        from src.utils.timezone import utc_now

        active = sum(
            1
            for agent in agents
            if agent.get("status") not in ("idle", "disconnected", "error")
        )
        alerts = sum(
            1
            for agent in agents
            if agent.get("alertLevel") in ("warning", "error")
        )

        summary: dict[str, Any] = {
            "activeAgents": active,
            "alertCount": alerts,
            "health": _overall_health(agents),
            "updatedAt": utc_now().isoformat(),
        }

        for provider in self._summary_providers:
            try:
                summary.update(provider())
            except Exception:
                logger.debug("Summary provider failed", exc_info=True)

        return summary

    def build_snapshot(self, *, force_refresh: bool = False) -> dict[str, Any]:
        snapshot = self.refresh_snapshot(force=force_refresh)
        return {
            "agents": snapshot["agents"],
            "events": self.recent_events(50),
            "summary": snapshot["summary"],
        }

    def emit_event(self, event: dict[str, Any]) -> None:
        self._event_buffer.append(event)
        self._broadcast({"type": "event_append", "payload": event})

    def subscribe(
        self,
        loop: asyncio.AbstractEventLoop,
    ) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        with self._sub_lock:
            self._subscribers.append((loop, queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._sub_lock:
            self._subscribers = [
                (loop, q) for loop, q in self._subscribers if q is not queue
            ]

    @property
    def subscriber_count(self) -> int:
        with self._sub_lock:
            return len(self._subscribers)

    def _broadcast(self, msg: dict[str, Any]) -> None:
        with self._sub_lock:
            dead: list[asyncio.Queue[dict[str, Any]]] = []
            for loop, queue in self._subscribers:
                try:
                    loop.call_soon_threadsafe(self._put_nowait_safely, queue, msg)
                except RuntimeError:
                    dead.append(queue)
            if dead:
                self._subscribers = [
                    (loop, q) for loop, q in self._subscribers if q not in dead
                ]

    @staticmethod
    def _put_nowait_safely(
        queue: asyncio.Queue[dict[str, Any]],
        msg: dict[str, Any],
    ) -> None:
        try:
            queue.put_nowait(msg)
        except asyncio.QueueFull:
            pass


def _overall_health(agents: list[dict[str, Any]]) -> str:
    has_error = any(agent.get("alertLevel") == "error" for agent in agents)
    has_warning = any(agent.get("alertLevel") == "warning" for agent in agents)
    if has_error:
        return "unhealthy"
    if has_warning:
        return "degraded"
    return "healthy"
