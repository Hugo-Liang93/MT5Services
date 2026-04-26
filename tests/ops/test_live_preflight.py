from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.ops.cli import live_preflight
from src.ops import mt5_session_gate


def test_ensure_mt5_session_gate_or_raise_surfaces_error_code(monkeypatch) -> None:
    settings = SimpleNamespace(instance_name="live-main", account_alias="live_main")
    state = SimpleNamespace(
        session_ready=False,
        error_code="interactive_login_required",
        error_message="terminal needs manual unlock/login",
    )

    monkeypatch.setattr(
        mt5_session_gate,
        "probe_mt5_session_gate",
        lambda instance_name=None, auto_launch_terminal=True: (settings, state),
    )

    with pytest.raises(RuntimeError, match="interactive_login_required"):
        mt5_session_gate.ensure_mt5_session_gate_or_raise(instance_name="live-main")


def test_ensure_topology_group_mt5_session_gate_or_raise_validates_all_instances(
    monkeypatch,
) -> None:
    group = SimpleNamespace(
        name="live",
        main="live-main",
        workers=["live-exec-a"],
        environment="live",
    )

    monkeypatch.setattr("src.config.topology.load_topology_group", lambda group_name: group)
    monkeypatch.setattr(
        mt5_session_gate,
        "ensure_mt5_session_gate_or_raise",
        lambda instance_name=None, auto_launch_terminal=True: (
            SimpleNamespace(account_alias=instance_name, instance_name=instance_name),
            SimpleNamespace(to_dict=lambda: {"session_ready": True, "error_code": None}),
        ),
    )

    payload = mt5_session_gate.ensure_topology_group_mt5_session_gate_or_raise("live")

    assert sorted(payload.keys()) == ["live-exec-a", "live-main"]
    assert payload["live-main"]["state"]["session_ready"] is True


def test_check_mt5_reports_interactive_login_required(monkeypatch) -> None:
    fake_settings = SimpleNamespace(
        mt5_path="C:/Program Files/TradeMax Global MT5 Terminal/terminal64.exe",
        mt5_server="TradeMaxGlobal-Live",
        mt5_login=60067107,
        mt5_password="secret",
        timezone="UTC",
        server_time_offset_hours=None,
    )
    fake_state = SimpleNamespace(
        terminal_reachable=True,
        terminal_process_ready=True,
        ipc_ready=False,
        authorized=False,
        account_match=False,
        session_ready=False,
        interactive_login_required=True,
        error_code="interactive_login_required",
        error_message="terminal needs manual unlock/login",
    )

    monkeypatch.setattr(
        "src.config.mt5.load_mt5_settings",
        lambda instance_name=None: fake_settings,
    )
    monkeypatch.setattr(
        "src.clients.base.MT5BaseClient.inspect_session_state",
        lambda self, **kwargs: fake_state,
    )
    monkeypatch.setattr("src.clients.base.mt5", None)

    rows = live_preflight._check_mt5_instance()

    assert any(
        name == "MT5 session gate"
        and status == "FAIL"
        and "interactive_login_required" in detail
        for name, status, detail in rows
    )


def test_check_mt5_uses_environment_topology_group(monkeypatch) -> None:
    group = SimpleNamespace(main="live-main", workers=["live-exec-a"])
    calls: list[str] = []

    monkeypatch.setattr("src.config.topology.load_topology_group", lambda group_name: group)
    monkeypatch.setattr(
        live_preflight,
        "_check_mt5_instance",
        lambda instance_name=None: calls.append(instance_name or "default") or [],
    )

    live_preflight._check_mt5("live")

    assert calls == ["live-main", "live-exec-a"]


# ---------------------------------------------------------------------------
# _check_api — regression: was probing removed /v1/health route → constant 404
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


def test_check_api_hits_root_health_not_v1_health(monkeypatch) -> None:
    """回归：旧实现请求 /v1/health（已不存在），稳定 404 → preflight 误报 FAIL。

    当前路由树：根 /health（业务级 ApiResponse）+ /v1/monitoring/health{,/live,/ready}。
    preflight 应走 /health（root），它返回 ApiResponse[dict] 含 mode/market/trading。
    """
    captured: list[str] = []

    def fake_get(url: str, timeout: float = 5) -> _FakeResp:
        captured.append(url)
        return _FakeResp(
            200,
            {
                "success": True,
                "data": {"mode": "FULL", "market": {"connected": True}},
            },
        )

    import requests as _req  # imported inside _check_api; patch module attr

    monkeypatch.setattr(_req, "get", fake_get)

    rows = live_preflight._check_api(api_base="http://localhost:8808")

    assert captured == ["http://localhost:8808/health"], (
        "_check_api 必须请求 root /health（business-level ApiResponse），"
        f"非已删除的 /v1/health；实际请求 {captured!r}"
    )
    assert rows
    name, status, detail = rows[0]
    assert name == "API health"
    assert status == "OK"


def test_check_api_extracts_mode_from_response(monkeypatch) -> None:
    """回归：旧实现读 data.status（无该字段）始终输出 'unknown'。

    新实现应至少能反映 runtime mode（FULL/OBSERVE/RISK_OFF/INGEST_ONLY）。
    """
    import requests as _req

    monkeypatch.setattr(
        _req,
        "get",
        lambda url, timeout=5: _FakeResp(
            200,
            {
                "success": True,
                "data": {
                    "mode": "OBSERVE",
                    "market": {"connected": True},
                    "trading": {"running": False},
                },
            },
        ),
    )

    rows = live_preflight._check_api()
    _, status, detail = rows[0]
    assert status == "OK"
    assert "OBSERVE" in detail, (
        f"detail 应反映 runtime mode，旧实现死写 'unknown'；实际 {detail!r}"
    )


def test_check_api_handles_connection_error(monkeypatch) -> None:
    """ConnectionError → SKIP（service not yet running 是合法 preflight 场景）。"""
    import requests as _req

    def boom(url: str, timeout: float = 5):
        raise _req.ConnectionError("conn refused")

    monkeypatch.setattr(_req, "get", boom)

    rows = live_preflight._check_api()
    _, status, _ = rows[0]
    assert status == "SKIP"


def test_check_api_marks_failure_when_success_false(monkeypatch) -> None:
    """ApiResponse.success=False → FAIL（避免静默把异常状态当 OK）。"""
    import requests as _req

    monkeypatch.setattr(
        _req,
        "get",
        lambda url, timeout=5: _FakeResp(
            200,
            {"success": False, "error": {"code": "DEPS_NOT_READY"}},
        ),
    )

    rows = live_preflight._check_api()
    _, status, detail = rows[0]
    assert status == "FAIL"
    assert "DEPS_NOT_READY" in detail or "success=False" in detail
