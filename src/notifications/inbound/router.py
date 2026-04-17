"""Command router — parse Telegram updates into command invocations.

Takes raw ``update`` dicts from the poller and:
1. Extracts chat_id / text / username.
2. Parses ``/command [args]`` (with optional ``@botname`` suffix stripped).
3. Checks authz (reader vs admin vs unauthorized).
4. Enforces per-chat rate limit.
5. Dispatches to registered handler, gets back reply text.
6. Sends reply via the outbound transport.

Unknown / unauthorized / non-command messages are silently ignored (with a
DEBUG log). Errors inside handlers surface as a single generic reply; the
exception is swallowed so the poller loop doesn't die.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from src.notifications.inbound.authz import Authorizer, AuthzDecision, AuthzRole
from src.notifications.rate_limit import RateLimiter
from src.notifications.transport.base import NotificationTransport

logger = logging.getLogger(__name__)


Handler = Callable[[], str]


@dataclass(frozen=True)
class CommandRequest:
    chat_id: str
    username: str
    command: str
    args: tuple[str, ...]
    raw_text: str
    role: AuthzRole


@dataclass
class _HandlerRegistration:
    handler: Handler
    admin_only: bool = False


_COMMAND_PATTERN = re.compile(r"^/([A-Za-z][A-Za-z0-9\-_]*)(?:@[\w]+)?(.*)$", re.DOTALL)


def _parse_command(text: str) -> tuple[str, tuple[str, ...]] | None:
    if not text:
        return None
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    match = _COMMAND_PATTERN.match(stripped)
    if match is None:
        return None
    command = match.group(1).lower()
    rest = match.group(2).strip()
    args = tuple(rest.split()) if rest else ()
    return command, args


class CommandRouter:
    def __init__(
        self,
        *,
        authorizer: Authorizer,
        transport: NotificationTransport,
        rate_limiter: Optional[RateLimiter] = None,
        command_whitelist: Optional[tuple[str, ...]] = None,
    ) -> None:
        self._authorizer = authorizer
        self._transport = transport
        self._rate_limiter = rate_limiter
        self._handlers: dict[str, _HandlerRegistration] = {}
        # None or empty → all registered commands allowed. Non-empty tuple =
        # only those names may dispatch; anything else silently rejected.
        self._command_whitelist: Optional[frozenset[str]] = (
            frozenset(c.lower() for c in command_whitelist)
            if command_whitelist
            else None
        )

    def register(
        self,
        name: str,
        handler: Handler,
        *,
        admin_only: bool = False,
    ) -> None:
        key = name.lower().lstrip("/")
        self._handlers[key] = _HandlerRegistration(
            handler=handler, admin_only=admin_only
        )

    def registered_commands(self) -> list[str]:
        return sorted(self._handlers.keys())

    # ── entry point called by poller ──

    def dispatch(self, update: Mapping[str, Any]) -> None:
        """Process a single Telegram ``update`` dict. Never raises."""
        try:
            self._dispatch_impl(update)
        except Exception:  # noqa: BLE001 — poller depends on isolation
            logger.exception("command router dispatch failed; dropping update")

    def _dispatch_impl(self, update: Mapping[str, Any]) -> None:
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, Mapping):
            return
        chat = message.get("chat")
        if not isinstance(chat, Mapping):
            return
        chat_id = str(chat.get("id") or "").strip()
        if not chat_id:
            return
        text = str(message.get("text") or "").strip()
        username = self._extract_username(message)

        parsed = _parse_command(text)
        if parsed is None:
            # Non-command messages are intentionally silent (avoid chatty bot).
            return
        command, args = parsed

        # Whitelist gate: lets operators disable certain commands from config
        # without unregistering the handler. Useful for canary rollout.
        if (
            self._command_whitelist is not None
            and command not in self._command_whitelist
        ):
            logger.debug("command %s not in whitelist; ignored", command)
            return

        decision = self._authorizer.resolve(chat_id)
        if not decision.is_authorized:
            logger.info(
                "inbound command from unauthorized chat_id=%s username=%s cmd=%s — silently ignored",
                chat_id,
                username,
                command,
            )
            return

        # Rate limit per chat (not per user) — prevents command bombs.
        if self._rate_limiter is not None:
            verdict = self._rate_limiter.acquire(f"inbound:{chat_id}")
            if not verdict.allowed:
                logger.warning(
                    "inbound rate limit hit chat_id=%s — dropping cmd=%s",
                    chat_id,
                    command,
                )
                return

        registration = self._handlers.get(command)
        if registration is None:
            self._reply(
                chat_id,
                f"未知命令：`/{command}` — 发送 `/help` 查看可用命令",
            )
            return

        if registration.admin_only and not decision.is_admin:
            self._reply(chat_id, "❌ 该命令仅限 admin_chat_ids 执行")
            return

        request = CommandRequest(
            chat_id=chat_id,
            username=username,
            command=command,
            args=args,
            raw_text=text,
            role=decision.role,
        )
        self._invoke(registration, request)

    def _invoke(
        self, registration: _HandlerRegistration, request: CommandRequest
    ) -> None:
        try:
            reply = registration.handler()
        except Exception:  # noqa: BLE001
            logger.exception("handler for %s raised", request.command)
            self._reply(
                request.chat_id, f"❌ 执行 `/{request.command}` 时出错（见服务端日志）"
            )
            return
        if not reply:
            logger.debug("handler for %s returned empty reply", request.command)
            return
        self._reply(request.chat_id, reply)

    def _reply(self, chat_id: str, text: str) -> None:
        try:
            result = self._transport.send(chat_id=chat_id, text=text)
        except Exception:  # noqa: BLE001
            logger.exception("transport.send raised replying to chat %s", chat_id)
            return
        if not result.ok:
            logger.warning(
                "reply to chat=%s failed: retryable=%s err=%s",
                chat_id,
                result.retryable,
                result.error,
            )

    @staticmethod
    def _extract_username(message: Mapping[str, Any]) -> str:
        sender = message.get("from")
        if isinstance(sender, Mapping):
            username = sender.get("username")
            if username:
                return str(username)
            full = str(sender.get("first_name") or "").strip()
            if sender.get("last_name"):
                full = (full + " " + str(sender.get("last_name"))).strip()
            if full:
                return full
        return ""
