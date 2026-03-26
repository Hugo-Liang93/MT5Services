"""StudioService — aggregation layer with registry pattern.

Core design principles:
    1. **Zero business imports** — this module does not import any trading,
       signal, or ingestion module.  All data arrives through registered
       callables (``Callable[[], dict]``).
    2. **Registry pattern** — agent status providers and summary providers
       are registered at build time (``builder.py``) via simple callables.
    3. **Thread-safe SSE fan-out** — background threads push events via
       :meth:`emit_event`; SSE handlers receive them through per-connection
       asyncio queues bridged with ``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable

from .event_buffer import EventBuffer
from .models import build_agent

logger = logging.getLogger(__name__)


class StudioService:
    """Central aggregation service for the Studio observability layer.

    Usage::

        studio = StudioService()

        # In builder.py — register providers (closures over modules)
        studio.register_agent("collector", lambda: map_collector(...))
        studio.register_summary_provider(lambda: {"account": "12345"})

        # In signal listener — push events from any thread
        studio.emit_event(build_event("signal_generated", ...))

        # In SSE endpoint — subscribe to real-time updates
        queue = studio.subscribe(asyncio.get_running_loop())
        try:
            ...  # yield from queue
        finally:
            studio.unsubscribe(queue)
    """

    def __init__(self, event_buffer_size: int = 200) -> None:
        # Agent provider registry: agent_id → callable returning StudioAgent dict
        self._agent_providers: dict[str, Callable[[], dict[str, Any]]] = {}
        # Summary data providers: each returns a partial dict merged into summary
        self._summary_providers: list[Callable[[], dict[str, Any]]] = []
        # Event ring buffer (thread-safe, in-memory only)
        self._event_buffer = EventBuffer(event_buffer_size)
        # SSE subscriber queues: (event_loop, queue) pairs
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]] = []
        self._sub_lock = threading.Lock()

    # ── Agent registry ─────────────────────────────────────────

    def register_agent(
        self,
        agent_id: str,
        provider: Callable[[], dict[str, Any]],
    ) -> None:
        """Register a status provider for one agent role.

        Parameters
        ----------
        agent_id:
            Must match a key in ``models.AGENT_META`` and the frontend's
            ``config/employees.ts``.
        provider:
            A zero-arg callable that returns a ``StudioAgent`` dict.
            Typically a closure created in ``builder.py`` that captures a
            module reference and calls a mapper function.
        """
        self._agent_providers[agent_id] = provider

    def register_summary_provider(
        self,
        provider: Callable[[], dict[str, Any]],
    ) -> None:
        """Register a provider that contributes fields to the summary.

        Multiple providers are merged (later overwrites earlier on conflict).
        """
        self._summary_providers.append(provider)

    # ── Read API (called by REST / SSE endpoints) ──────────────

    def build_agents(self) -> list[dict[str, Any]]:
        """Build current status for all registered agents.

        Each provider is called independently; a failing provider produces
        an ``error`` agent rather than crashing the entire response.
        """
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

    def build_summary(self) -> dict[str, Any]:
        """Build a summary object for the frontend TopBar."""
        from src.utils.timezone import utc_now

        agents = self.build_agents()
        active = sum(
            1 for a in agents
            if a.get("status") not in ("idle", "disconnected", "error")
        )
        alerts = sum(
            1 for a in agents
            if a.get("alertLevel") in ("warning", "error")
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

    def build_snapshot(self) -> dict[str, Any]:
        """Full snapshot for SSE initial push."""
        return {
            "agents": self.build_agents(),
            "events": self.recent_events(50),
            "summary": self.build_summary(),
        }

    # ── Event emission (thread-safe, called from any thread) ───

    def emit_event(self, event: dict[str, Any]) -> None:
        """Record an event and broadcast to all SSE subscribers.

        Safe to call from background threads (SignalRuntime, TradeExecutor, etc.).
        Uses ``call_soon_threadsafe`` to bridge into each subscriber's event loop.
        """
        self._event_buffer.append(event)
        self._broadcast({"type": "event_append", "payload": event})

    # ── SSE subscriber management ──────────────────────────────

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue[dict[str, Any]]:
        """Create a new SSE subscriber queue tied to *loop*."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        with self._sub_lock:
            self._subscribers.append((loop, queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove a subscriber queue."""
        with self._sub_lock:
            self._subscribers = [
                (loop, q) for loop, q in self._subscribers if q is not queue
            ]

    @property
    def subscriber_count(self) -> int:
        with self._sub_lock:
            return len(self._subscribers)

    # ── Internal ───────────────────────────────────────────────

    def _broadcast(self, msg: dict[str, Any]) -> None:
        """Push a message to all SSE subscriber queues (thread-safe)."""
        with self._sub_lock:
            dead: list[asyncio.Queue[dict[str, Any]]] = []
            for loop, queue in self._subscribers:
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, msg)
                except RuntimeError:
                    # Event loop closed — subscriber is gone
                    dead.append(queue)
                except asyncio.QueueFull:
                    pass  # Best-effort: drop if subscriber is slow
            if dead:
                self._subscribers = [
                    (loop, q) for loop, q in self._subscribers if q not in dead
                ]


def _overall_health(agents: list[dict[str, Any]]) -> str:
    """Derive overall health string from agent alert levels."""
    has_error = any(a.get("alertLevel") == "error" for a in agents)
    has_warning = any(a.get("alertLevel") == "warning" for a in agents)
    if has_error:
        return "unhealthy"
    if has_warning:
        return "degraded"
    return "healthy"
