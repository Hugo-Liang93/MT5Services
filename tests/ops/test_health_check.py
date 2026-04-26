"""Regression tests for src/ops/cli/health_check.py — trade module probe.

历史 bug: _check_runtime_modules() 请求 /v1/trade/overview（已删除路由）→
长期走异常分支 → "Trade executor: WARN unavailable: HTTP Error 404" →
健康检查无法反映 executor / circuit breaker 真实状态。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError

import pytest

from src.ops.cli import health_check


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN001
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


_BASE_ROUTES: dict[str, dict[str, Any]] = {
    # ready 探针必须 ready 否则 _run_checks 提前 return
    "/v1/monitoring/health/ready": {"status": "ready", "checks": {}},
    "/v1/monitoring/health": {
        "data": {"overall_status": "healthy", "active_alerts": {}, "latest_metrics": {}}
    },
    "/v1/monitoring/performance": {"data": {"status": "ok"}},
}


def _patch_fetch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    routes: dict[str, dict[str, Any]],
    captured: list[str],
) -> None:
    """Patch _fetch_json to serve route → payload map; record all calls.

    routes 自动合并 _BASE_ROUTES（ready/health/performance），调用方只需补
    要测的 trade route 即可。
    """
    merged = dict(_BASE_ROUTES)
    merged.update(routes)

    def fake_fetch(url: str, timeout: float = 5.0) -> dict[str, Any]:
        captured.append(url)
        for path, payload in merged.items():
            if url.endswith(path):
                return payload
        raise HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(health_check, "_fetch_json", fake_fetch)


def test_check_runtime_modules_hits_trade_control_not_overview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归：旧实现请求 /v1/trade/overview（已删），稳定 404 → WARN 无信息。

    正确路径：/v1/trade/control 返回 TradeControlStatusView，含 executor dict。
    """
    captured: list[str] = []
    _patch_fetch(
        monkeypatch,
        routes={
            "/v1/trade/control": {
                "data": {
                    "executor": {
                        "enabled": True,
                        "execution_count": 17,
                        "circuit_breaker": {"open": False},
                    }
                }
            },
        },
        captured=captured,
    )

    rows = health_check._run_checks("localhost", 8808)

    # 1. 必须请求 /v1/trade/control，禁止再请求 /v1/trade/overview
    trade_calls = [u for u in captured if "/trade/" in u]
    assert any(
        u.endswith("/v1/trade/control") for u in trade_calls
    ), f"应请求 /v1/trade/control，实际 trade calls: {trade_calls!r}"
    assert not any(
        u.endswith("/v1/trade/overview") for u in trade_calls
    ), f"禁止请求已删除的 /v1/trade/overview，实际 {trade_calls!r}"

    # 2. executor 状态必须真实反映而非 "unavailable"
    trade_row = next(
        (r for r in rows if r[0] == "Trade executor"),
        None,
    )
    assert trade_row is not None, "应有 Trade executor 检查项"
    name, status, detail = trade_row
    assert status == "OK"
    assert "enabled" in detail and "17" in detail


def test_check_runtime_modules_marks_trade_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """executor.enabled=False → WARN（非 unavailable，反映真实状态）。"""
    _patch_fetch(
        monkeypatch,
        routes={
            "/v1/trade/control": {
                "data": {"executor": {"enabled": False, "execution_count": 0}}
            },
        },
        captured=[],
    )

    rows = health_check._run_checks("localhost", 8808)
    trade_row = next((r for r in rows if r[0] == "Trade executor"), None)
    assert trade_row is not None
    _, status, detail = trade_row
    assert status == "WARN"
    assert "disabled" in detail.lower()


def test_check_runtime_modules_handles_real_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """endpoint 真返回 404 才标 WARN unavailable（与"路径写错恒 404"区分）。

    场景：ready/health/perf 都 OK，但 trade/control 因服务降级真 404。
    断言路径仍是 /v1/trade/control（防 URL 回退）+ WARN。
    """
    captured: list[str] = []
    # 仅 trade/control 不在 routes，触发 404；其他 base 路由仍 OK
    _patch_fetch(monkeypatch, routes={}, captured=captured)

    rows = health_check._run_checks("localhost", 8808)
    trade_row = next((r for r in rows if r[0] == "Trade executor"), None)
    assert any(u.endswith("/v1/trade/control") for u in captured)
    assert trade_row is not None and trade_row[1] == "WARN"
