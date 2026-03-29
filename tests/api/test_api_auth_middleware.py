from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi.responses import JSONResponse
from starlette.requests import Request

from src.api import api_key_authentication


def _request(
    path: str,
    headers: list[tuple[bytes, bytes]] | None = None,
    query_string: bytes = b"",
) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": headers or [],
        "query_string": query_string,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


def test_api_key_authentication_reads_current_config_per_request(monkeypatch) -> None:
    state = {
        "config": SimpleNamespace(
            auth_enabled=True,
            api_key="first-key",
            api_key_header="X-API-Key",
        )
    }

    monkeypatch.setattr("src.api.get_api_config", lambda: state["config"])

    async def call_next(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True}, status_code=200)

    first = asyncio.run(
        api_key_authentication(
            _request("/symbols", [(b"x-api-key", b"first-key")]),
            call_next,
        )
    )
    assert first.status_code == 200

    state["config"] = SimpleNamespace(
        auth_enabled=True,
        api_key="second-key",
        api_key_header="X-Reloaded-Key",
    )

    second = asyncio.run(
        api_key_authentication(
            _request("/symbols", [(b"x-reloaded-key", b"second-key")]),
            call_next,
        )
    )
    assert second.status_code == 200


def test_api_key_authentication_rejects_when_reloaded_key_changes(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.api.get_api_config",
        lambda: SimpleNamespace(
            auth_enabled=True,
            api_key="expected-key",
            api_key_header="X-API-Key",
        ),
    )

    async def call_next(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True}, status_code=200)

    response = asyncio.run(
        api_key_authentication(
            _request("/symbols", [(b"x-api-key", b"stale-key")]),
            call_next,
        )
    )

    assert response.status_code == 401


def test_api_key_authentication_accepts_sse_query_param(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.api.get_api_config",
        lambda: SimpleNamespace(
            auth_enabled=True,
            api_key="stream-key",
            api_key_header="X-API-Key",
        ),
    )

    async def call_next(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True}, status_code=200)

    response = asyncio.run(
        api_key_authentication(
            _request("/v1/studio/stream", query_string=b"api_key=stream-key"),
            call_next,
        )
    )

    assert response.status_code == 200
