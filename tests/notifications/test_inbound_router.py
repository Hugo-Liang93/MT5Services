"""CommandRouter dispatch + authz + rate-limit + reply plumbing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from src.notifications.inbound.authz import Authorizer
from src.notifications.inbound.router import CommandRouter, _parse_command
from src.notifications.rate_limit import RateLimiter
from src.notifications.transport.base import NotificationTransport, TransportResult


@dataclass
class CapturingTransport(NotificationTransport):
    sent: List[tuple[str, str]] = field(default_factory=list)
    fail_next: bool = False

    def send(self, *, chat_id: str, text: str) -> TransportResult:
        self.sent.append((chat_id, text))
        if self.fail_next:
            self.fail_next = False
            return TransportResult(ok=False, retryable=False, error="mock failure")
        return TransportResult(ok=True)


def _update(
    chat_id: str | int = "100", text: str = "/health", username: str = "alice"
) -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": chat_id, "username": username},
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }


class TestParse:
    def test_simple(self):
        assert _parse_command("/health") == ("health", ())

    def test_with_args(self):
        assert _parse_command("/trace  abc123  ") == ("trace", ("abc123",))

    def test_botname_suffix_stripped(self):
        assert _parse_command("/health@MyBot foo") == ("health", ("foo",))

    def test_non_command(self):
        assert _parse_command("hello") is None
        assert _parse_command("") is None
        assert _parse_command("   ") is None

    def test_case_lowered(self):
        assert _parse_command("/Health") == ("health", ())


class TestAuthzGate:
    def _router(
        self, admins=("100",), allowed=("100", "200")
    ) -> tuple[CommandRouter, CapturingTransport]:
        transport = CapturingTransport()
        az = Authorizer(allowed_chat_ids=allowed, admin_chat_ids=admins)
        router = CommandRouter(authorizer=az, transport=transport)
        router.register("health", lambda: "OK")
        router.register("halt", lambda: "halted", admin_only=True)
        return router, transport

    def test_authorized_reader_dispatches(self):
        router, transport = self._router()
        router.dispatch(_update(chat_id="200", text="/health"))
        assert len(transport.sent) == 1
        assert transport.sent[0] == ("200", "OK")

    def test_unauthorized_chat_silently_ignored(self):
        router, transport = self._router()
        router.dispatch(_update(chat_id="999", text="/health"))
        assert transport.sent == []

    def test_admin_only_rejects_reader(self):
        router, transport = self._router()
        router.dispatch(_update(chat_id="200", text="/halt"))
        assert len(transport.sent) == 1
        assert "admin" in transport.sent[0][1]

    def test_admin_only_accepts_admin(self):
        router, transport = self._router()
        router.dispatch(_update(chat_id="100", text="/halt"))
        assert transport.sent == [("100", "halted")]


class TestWhitelist:
    def test_non_whitelisted_ignored(self):
        transport = CapturingTransport()
        az = Authorizer(allowed_chat_ids=["100"], admin_chat_ids=[])
        router = CommandRouter(
            authorizer=az,
            transport=transport,
            command_whitelist=("health",),
        )
        router.register("health", lambda: "OK")
        router.register("positions", lambda: "POS")
        router.dispatch(_update(chat_id="100", text="/positions"))
        assert transport.sent == []

    def test_whitelisted_allowed(self):
        transport = CapturingTransport()
        az = Authorizer(allowed_chat_ids=["100"], admin_chat_ids=[])
        router = CommandRouter(
            authorizer=az,
            transport=transport,
            command_whitelist=("health",),
        )
        router.register("health", lambda: "OK")
        router.dispatch(_update(chat_id="100", text="/health"))
        assert len(transport.sent) == 1


class TestRateLimit:
    def test_limit_drops(self):
        transport = CapturingTransport()
        az = Authorizer(allowed_chat_ids=["100"], admin_chat_ids=[])
        # 2 per minute inbound means the 3rd hit gets dropped.
        rl = RateLimiter(global_per_minute=1000, per_chat_per_minute=2)
        router = CommandRouter(authorizer=az, transport=transport, rate_limiter=rl)
        router.register("health", lambda: "OK")
        for _ in range(3):
            router.dispatch(_update(chat_id="100", text="/health"))
        # Only the first 2 pass through.
        assert len(transport.sent) == 2


class TestMalformedUpdates:
    def test_non_command_ignored(self):
        transport = CapturingTransport()
        az = Authorizer(allowed_chat_ids=["100"], admin_chat_ids=[])
        router = CommandRouter(authorizer=az, transport=transport)
        router.register("health", lambda: "OK")
        router.dispatch(_update(chat_id="100", text="random chat"))
        assert transport.sent == []

    def test_missing_chat_ignored(self):
        transport = CapturingTransport()
        az = Authorizer(allowed_chat_ids=["100"], admin_chat_ids=[])
        router = CommandRouter(authorizer=az, transport=transport)
        router.register("health", lambda: "OK")
        router.dispatch({"update_id": 1, "message": {"text": "/health"}})  # no chat
        assert transport.sent == []

    def test_empty_update_ignored(self):
        transport = CapturingTransport()
        az = Authorizer(allowed_chat_ids=["100"], admin_chat_ids=[])
        router = CommandRouter(authorizer=az, transport=transport)
        router.dispatch({})
        assert transport.sent == []


class TestHandlerFailures:
    def test_unknown_command_friendly_reply(self):
        transport = CapturingTransport()
        az = Authorizer(allowed_chat_ids=["100"], admin_chat_ids=[])
        router = CommandRouter(authorizer=az, transport=transport)
        router.register("health", lambda: "OK")
        router.dispatch(_update(chat_id="100", text="/nope"))
        assert len(transport.sent) == 1
        assert "未知命令" in transport.sent[0][1]

    def test_handler_exception_returns_error_reply(self):
        transport = CapturingTransport()
        az = Authorizer(allowed_chat_ids=["100"], admin_chat_ids=[])
        router = CommandRouter(authorizer=az, transport=transport)

        def _boom():
            raise RuntimeError("x")

        router.register("health", _boom)
        router.dispatch(_update(chat_id="100", text="/health"))
        assert len(transport.sent) == 1
        assert "出错" in transport.sent[0][1]

    def test_empty_reply_not_sent(self):
        transport = CapturingTransport()
        az = Authorizer(allowed_chat_ids=["100"], admin_chat_ids=[])
        router = CommandRouter(authorizer=az, transport=transport)
        router.register("health", lambda: "")
        router.dispatch(_update(chat_id="100", text="/health"))
        assert transport.sent == []

    def test_transport_failure_logged_not_raised(self):
        transport = CapturingTransport(fail_next=True)
        az = Authorizer(allowed_chat_ids=["100"], admin_chat_ids=[])
        router = CommandRouter(authorizer=az, transport=transport)
        router.register("health", lambda: "OK")
        # Must not raise, even though transport returned ok=False.
        router.dispatch(_update(chat_id="100", text="/health"))

    def test_dispatch_never_raises(self):
        """Defensive: router.dispatch is called in the poller loop — any
        raise would stall polling until the next iteration. Test a
        deliberately broken update shape."""
        transport = CapturingTransport()
        az = Authorizer(allowed_chat_ids=["100"], admin_chat_ids=[])
        router = CommandRouter(authorizer=az, transport=transport)
        router.register("health", lambda: "OK")
        # Passing a list where dict expected — must be absorbed.
        router.dispatch([1, 2, 3])  # type: ignore[arg-type]
