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
        lambda instance_name=None: (settings, state),
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
        lambda instance_name=None: (
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
