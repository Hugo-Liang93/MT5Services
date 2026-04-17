"""Unit tests for TelegramTransport.

Uses an injected ``requests.Session`` via ``unittest.mock.MagicMock`` instead
of ``requests-mock`` so we don't pull in a new test-time dependency.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests
from pydantic import SecretStr

from src.notifications.transport.telegram import TelegramTransport


def _mock_response(status: int, body: dict | None = None) -> MagicMock:
    response = MagicMock(spec=requests.Response)
    response.status_code = status
    response.json.return_value = body if body is not None else {}
    response.text = str(body or "")
    return response


def _make_transport(session: MagicMock, **kwargs) -> TelegramTransport:
    return TelegramTransport(
        bot_token=SecretStr("fake:token"),
        session=session,
        **kwargs,
    )


class TestSend:
    def test_success_200_ok_true(self):
        session = MagicMock(spec=requests.Session)
        session.post.return_value = _mock_response(200, {"ok": True, "result": {}})
        transport = _make_transport(session)

        result = transport.send(chat_id="123", text="hello")
        assert result.ok is True
        assert result.retryable is False
        assert result.error is None
        # Verify URL assembly uses the token
        call_args = session.post.call_args
        assert "bot" in call_args.args[0] or "bot" in call_args.kwargs.get("url", "")
        url = call_args.args[0] if call_args.args else call_args.kwargs["url"]
        assert "bot" in url and "fake:token" in url

    def test_200_but_ok_false_terminal(self):
        session = MagicMock(spec=requests.Session)
        session.post.return_value = _mock_response(
            200, {"ok": False, "description": "invalid chat"}
        )
        transport = _make_transport(session)
        result = transport.send(chat_id="123", text="hi")
        assert result.ok is False
        assert result.retryable is False  # terminal: remote rejected
        assert "telegram refused" in (result.error or "")

    def test_429_retryable_with_retry_after(self):
        session = MagicMock(spec=requests.Session)
        session.post.return_value = _mock_response(
            429,
            {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 12},
            },
        )
        transport = _make_transport(session)
        result = transport.send(chat_id="123", text="hi")
        assert result.ok is False
        assert result.retryable is True
        assert result.retry_after_seconds == 12.0

    def test_500_retryable(self):
        session = MagicMock(spec=requests.Session)
        session.post.return_value = _mock_response(500, {"ok": False})
        transport = _make_transport(session)
        result = transport.send(chat_id="123", text="hi")
        assert result.ok is False
        assert result.retryable is True
        assert result.retry_after_seconds is None

    def test_400_non_retryable(self):
        session = MagicMock(spec=requests.Session)
        session.post.return_value = _mock_response(
            400, {"ok": False, "description": "bad request"}
        )
        transport = _make_transport(session)
        result = transport.send(chat_id="123", text="hi")
        assert result.ok is False
        assert result.retryable is False

    def test_401_non_retryable(self):
        session = MagicMock(spec=requests.Session)
        session.post.return_value = _mock_response(
            401, {"ok": False, "description": "unauthorized"}
        )
        transport = _make_transport(session)
        result = transport.send(chat_id="123", text="hi")
        assert result.ok is False
        assert result.retryable is False

    def test_timeout_retryable(self):
        session = MagicMock(spec=requests.Session)
        session.post.side_effect = requests.Timeout("slow")
        transport = _make_transport(session)
        result = transport.send(chat_id="123", text="hi")
        assert result.ok is False
        assert result.retryable is True
        assert "timeout" in (result.error or "")

    def test_connection_error_retryable(self):
        session = MagicMock(spec=requests.Session)
        session.post.side_effect = requests.ConnectionError("dns fail")
        transport = _make_transport(session)
        result = transport.send(chat_id="123", text="hi")
        assert result.ok is False
        assert result.retryable is True

    def test_generic_request_error_retryable(self):
        session = MagicMock(spec=requests.Session)
        session.post.side_effect = requests.RequestException("something")
        transport = _make_transport(session)
        result = transport.send(chat_id="123", text="hi")
        assert result.ok is False
        assert result.retryable is True

    def test_empty_chat_id_rejected_locally(self):
        session = MagicMock(spec=requests.Session)
        transport = _make_transport(session)
        result = transport.send(chat_id="", text="hi")
        assert result.ok is False
        assert result.retryable is False
        # Session.post never called
        session.post.assert_not_called()

    def test_empty_text_rejected_locally(self):
        session = MagicMock(spec=requests.Session)
        transport = _make_transport(session)
        result = transport.send(chat_id="123", text="")
        assert result.ok is False
        assert result.retryable is False
        session.post.assert_not_called()

    def test_proxy_passed_through(self):
        session = MagicMock(spec=requests.Session)
        session.post.return_value = _mock_response(200, {"ok": True})
        transport = _make_transport(session, proxy_url="http://proxy:8080")
        transport.send(chat_id="1", text="x")
        kwargs = session.post.call_args.kwargs
        assert kwargs["proxies"] == {
            "http": "http://proxy:8080",
            "https": "http://proxy:8080",
        }

    def test_no_proxy_means_none(self):
        session = MagicMock(spec=requests.Session)
        session.post.return_value = _mock_response(200, {"ok": True})
        transport = _make_transport(session)
        transport.send(chat_id="1", text="x")
        kwargs = session.post.call_args.kwargs
        assert kwargs["proxies"] is None

    def test_request_payload_shape(self):
        session = MagicMock(spec=requests.Session)
        session.post.return_value = _mock_response(200, {"ok": True})
        transport = _make_transport(session)
        transport.send(chat_id="99", text="hello *world*")
        body = session.post.call_args.kwargs["json"]
        assert body["chat_id"] == "99"
        assert body["text"] == "hello *world*"
        assert body["parse_mode"] == "Markdown"
        assert body["disable_web_page_preview"] is True

    def test_accepts_string_token(self):
        session = MagicMock(spec=requests.Session)
        session.post.return_value = _mock_response(200, {"ok": True})
        transport = TelegramTransport(bot_token="str:token", session=session)
        assert transport.send(chat_id="1", text="x").ok is True

    def test_empty_token_rejected(self):
        with pytest.raises(ValueError, match="bot_token"):
            TelegramTransport(bot_token="")
        with pytest.raises(ValueError, match="bot_token"):
            TelegramTransport(bot_token=SecretStr(""))
