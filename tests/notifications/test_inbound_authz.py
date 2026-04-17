"""Authorizer chat_id → role resolution tests."""

from __future__ import annotations

from src.notifications.inbound.authz import Authorizer, AuthzRole


class TestAuthorizer:
    def test_admin_beats_reader(self):
        az = Authorizer(allowed_chat_ids=["100"], admin_chat_ids=["100"])
        assert az.resolve("100").role is AuthzRole.ADMIN
        assert az.resolve("100").is_admin is True
        assert az.resolve("100").is_authorized is True

    def test_reader_not_admin(self):
        az = Authorizer(allowed_chat_ids=["100", "200"], admin_chat_ids=["100"])
        decision = az.resolve("200")
        assert decision.role is AuthzRole.READER
        assert decision.is_admin is False
        assert decision.is_authorized is True

    def test_unauthorized_stranger(self):
        az = Authorizer(allowed_chat_ids=["100"], admin_chat_ids=[])
        decision = az.resolve("999")
        assert decision.role is AuthzRole.UNAUTHORIZED
        assert decision.is_authorized is False
        assert decision.is_admin is False

    def test_admin_implicitly_reader(self):
        # Admins appearing only in admin_chat_ids are still authorized
        # (admins are implicit readers) — prevents the trap where admin
        # can do control commands but not queries.
        az = Authorizer(allowed_chat_ids=[], admin_chat_ids=["999"])
        decision = az.resolve("999")
        assert decision.is_admin is True
        assert decision.is_authorized is True

    def test_int_chat_id_accepted(self):
        az = Authorizer(allowed_chat_ids=["100"], admin_chat_ids=[])
        assert az.resolve(100).is_authorized is True
        assert az.resolve("100").is_authorized is True

    def test_blank_rejected(self):
        az = Authorizer(allowed_chat_ids=["100"], admin_chat_ids=[])
        assert az.resolve("").is_authorized is False
        assert az.resolve("   ").is_authorized is False

    def test_whitespace_stripped(self):
        az = Authorizer(allowed_chat_ids=["  100  "], admin_chat_ids=[])
        assert az.resolve("100").is_authorized is True

    def test_counts(self):
        az = Authorizer(allowed_chat_ids=["100", "200", "300"], admin_chat_ids=["100"])
        assert az.admin_count() == 1
        assert az.reader_count() == 2
