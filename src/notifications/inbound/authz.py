"""Inbound authorization — who can invoke which command.

Two-tier model kept intentionally simple:
- ``allowed_chat_ids`` → read-only commands (health / positions / ...)
- ``admin_chat_ids``   → control commands (halt / resume / reload-config, Phase 4)

Decisions are taken *silently* for non-allowed chat_ids: no error reply,
nothing sent. Prevents bots from inadvertently enumerating command behavior
to strangers who discover the bot (a common Telegram scrape pattern).
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Iterable

logger = logging.getLogger(__name__)


class AuthzRole(str, enum.Enum):
    UNAUTHORIZED = "unauthorized"
    READER = "reader"
    ADMIN = "admin"


@dataclass(frozen=True)
class AuthzDecision:
    role: AuthzRole

    @property
    def is_authorized(self) -> bool:
        return self.role is not AuthzRole.UNAUTHORIZED

    @property
    def is_admin(self) -> bool:
        return self.role is AuthzRole.ADMIN


class Authorizer:
    """Evaluate (chat_id → role) given configured allow lists.

    Any chat_id present in ``admin_chat_ids`` is ADMIN.
    Any chat_id present in ``allowed_chat_ids`` (and not admin) is READER.
    Anything else is UNAUTHORIZED.
    """

    def __init__(
        self,
        *,
        allowed_chat_ids: Iterable[str],
        admin_chat_ids: Iterable[str],
    ) -> None:
        self._allowed = {
            str(cid).strip() for cid in allowed_chat_ids if str(cid).strip()
        }
        self._admins = {str(cid).strip() for cid in admin_chat_ids if str(cid).strip()}
        # Admins are implicitly readers; normalize so calling code doesn't
        # have to check both sets.
        self._allowed |= self._admins

    def resolve(self, chat_id: str | int) -> AuthzDecision:
        key = str(chat_id).strip()
        if not key:
            return AuthzDecision(role=AuthzRole.UNAUTHORIZED)
        if key in self._admins:
            return AuthzDecision(role=AuthzRole.ADMIN)
        if key in self._allowed:
            return AuthzDecision(role=AuthzRole.READER)
        return AuthzDecision(role=AuthzRole.UNAUTHORIZED)

    def admin_count(self) -> int:
        return len(self._admins)

    def reader_count(self) -> int:
        return len(self._allowed) - len(self._admins)
