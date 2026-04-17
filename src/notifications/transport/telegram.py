"""Telegram Bot API transport.

Notable behaviors:
- **Retryable vs non-retryable**: 429 (rate limit) + 5xx + network errors are
  retryable; 4xx (except 429) are treated as terminal and routed to DLQ on
  the next attempt bump. A malformed payload should surface loudly, not
  silently retry forever.
- **Respecting retry_after**: Telegram 429 responses include
  ``parameters.retry_after`` (seconds). When present we return it so the
  dispatcher can schedule the next attempt past that window, rather than
  using the local backoff ladder which could undershoot.
- **Token is held in a SecretStr**: we read it once at construction and never
  log it; URL assembly stays inside this module.
- **No async / no asyncio**: matches the project's sync threading model; the
  dispatcher owns a dedicated worker thread that calls ``send`` in a loop.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

import requests
from pydantic import SecretStr

from src.notifications.transport.base import NotificationTransport, TransportResult

logger = logging.getLogger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org"
_MARKDOWN_PARSE_MODE = "Markdown"


class TelegramTransport(NotificationTransport):
    def __init__(
        self,
        *,
        bot_token: SecretStr | str,
        timeout_seconds: float = 10.0,
        proxy_url: str | None = None,
        session: requests.Session | None = None,
        api_base: str = _TELEGRAM_API_BASE,
    ) -> None:
        token = (
            bot_token.get_secret_value()
            if isinstance(bot_token, SecretStr)
            else str(bot_token)
        )
        if not token.strip():
            raise ValueError("bot_token cannot be empty")
        self._token = token
        self._timeout = float(timeout_seconds)
        self._proxies: dict[str, str] | None = (
            {"http": proxy_url, "https": proxy_url} if proxy_url else None
        )
        self._session = session or requests.Session()
        self._api_base = api_base.rstrip("/")

    def _send_url(self) -> str:
        return f"{self._api_base}/bot{self._token}/sendMessage"

    def send(self, *, chat_id: str, text: str) -> TransportResult:
        if not chat_id:
            return TransportResult(ok=False, retryable=False, error="chat_id empty")
        if not text:
            return TransportResult(ok=False, retryable=False, error="text empty")
        payload: Mapping[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": _MARKDOWN_PARSE_MODE,
            "disable_web_page_preview": True,
        }
        try:
            response = self._session.post(
                self._send_url(),
                json=payload,
                timeout=self._timeout,
                proxies=self._proxies,
            )
        except requests.Timeout as exc:
            return TransportResult(
                ok=False,
                retryable=True,
                error=f"timeout: {exc}",
            )
        except requests.ConnectionError as exc:
            return TransportResult(
                ok=False,
                retryable=True,
                error=f"connection error: {exc}",
            )
        except requests.RequestException as exc:
            return TransportResult(
                ok=False,
                retryable=True,
                error=f"request failure: {exc}",
            )
        return _interpret_response(response)

    def get_updates(
        self,
        *,
        offset: int,
        timeout_seconds: int = 30,
    ) -> list[dict[str, Any]]:
        """Long-poll Telegram for new updates.

        Returns the list of ``update`` objects (possibly empty). Raises on
        network/HTTP errors — the caller (poller) retries with backoff.

        ``offset`` is the ``update_id`` water mark: pass ``last_update_id + 1``
        so already-processed updates are implicitly acknowledged and not
        redelivered. ``timeout_seconds`` is a *server-side* long-poll wait;
        the HTTP client gets ``timeout_seconds + 5`` to account for network
        jitter. This matches Telegram's recommended long-polling pattern.
        """
        url = f"{self._api_base}/bot{self._token}/getUpdates"
        params = {"offset": int(offset), "timeout": int(timeout_seconds)}
        response = self._session.get(
            url,
            params=params,
            timeout=float(timeout_seconds) + 5.0,
            proxies=self._proxies,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not body.get("ok"):
            raise RuntimeError(f"getUpdates non-ok response: {body}")
        result = body.get("result") or []
        if not isinstance(result, list):
            raise RuntimeError(f"getUpdates result malformed: {result!r}")
        return [item for item in result if isinstance(item, dict)]


def _interpret_response(response: requests.Response) -> TransportResult:
    status = response.status_code
    body = _safe_json(response)
    if 200 <= status < 300:
        if isinstance(body, dict) and body.get("ok") is True:
            return TransportResult(ok=True)
        # 2xx but payload says not ok — treat as terminal: retrying won't help.
        return TransportResult(
            ok=False,
            retryable=False,
            error=f"telegram refused: {body}",
        )
    if status == 429:
        retry_after = _extract_retry_after(body)
        return TransportResult(
            ok=False,
            retryable=True,
            error=f"rate limited by telegram (429): {body}",
            retry_after_seconds=retry_after,
        )
    if 500 <= status < 600:
        return TransportResult(
            ok=False,
            retryable=True,
            error=f"server error {status}: {body}",
        )
    # 4xx other than 429: permanent — bad chat_id, invalid token, bad markdown.
    return TransportResult(
        ok=False,
        retryable=False,
        error=f"client error {status}: {body}",
    )


def _safe_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"_raw_text": response.text[:500]}


def _extract_retry_after(body: Any) -> float | None:
    if not isinstance(body, dict):
        return None
    params = body.get("parameters")
    if isinstance(params, dict):
        value = params.get("retry_after")
        if isinstance(value, (int, float)):
            return float(value)
    return None
