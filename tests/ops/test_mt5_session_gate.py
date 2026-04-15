"""MT5 session gate 的 auto_launch_terminal 行为回归测试。

默认行为应该是"允许 mt5.initialize(path=...) 自动拉起 terminal"，
避免强制要求用户先手动开 MT5 terminal 才能启动服务。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.ops import mt5_session_gate


class _StubState:
    def __init__(self, *, session_ready: bool, error_code: str | None = None,
                 error_message: str | None = None) -> None:
        self.session_ready = session_ready
        self.error_code = error_code
        self.error_message = error_message

    def to_dict(self):
        return {"session_ready": self.session_ready, "error_code": self.error_code}


class _StubClient:
    """记录 inspect_session_state 调用参数供测试断言。"""
    def __init__(self, state: _StubState) -> None:
        self._state = state
        self.inspect_calls: list[dict] = []

    def inspect_session_state(self, **kwargs):
        self.inspect_calls.append(kwargs)
        return self._state


def _patch_gate(monkeypatch, state: _StubState) -> _StubClient:
    client = _StubClient(state)
    monkeypatch.setattr(
        mt5_session_gate,
        "MT5BaseClient",
        lambda settings: client,
        raising=False,
    )
    monkeypatch.setattr(
        mt5_session_gate,
        "load_mt5_settings",
        lambda instance_name=None: SimpleNamespace(
            instance_name=instance_name or "default",
            mt5_path="C:/MT5/terminal64.exe",
            mt5_login=12345,
            mt5_server="Demo-Server",
            account_alias="demo_main",
        ),
        raising=False,
    )
    # patch 两处 import（src.ops.mt5_session_gate 模块内部延迟 import）
    import src.clients.base as base_mod
    import src.config.mt5 as mt5_cfg_mod
    monkeypatch.setattr(base_mod, "MT5BaseClient", lambda settings: client, raising=False)
    monkeypatch.setattr(
        mt5_cfg_mod,
        "load_mt5_settings",
        lambda instance_name=None: SimpleNamespace(
            instance_name=instance_name or "default",
            mt5_path="C:/MT5/terminal64.exe",
            mt5_login=12345,
            mt5_server="Demo-Server",
            account_alias="demo_main",
        ),
        raising=False,
    )
    return client


def test_probe_gate_defaults_to_auto_launch(monkeypatch) -> None:
    """默认 auto_launch_terminal=True → require_terminal_process 传 False 给 inspect。"""
    client = _patch_gate(monkeypatch, _StubState(session_ready=True))

    _, state = mt5_session_gate.probe_mt5_session_gate(instance_name="demo-main")

    assert state.session_ready is True
    assert len(client.inspect_calls) == 1
    call = client.inspect_calls[0]
    # auto_launch=True（默认）→ require_terminal_process=False，让 initialize 自己拉起
    assert call["require_terminal_process"] is False
    assert call["attempt_initialize"] is True


def test_probe_gate_strict_mode_disables_auto_launch(monkeypatch) -> None:
    """auto_launch_terminal=False → require_terminal_process=True，严格模式。

    供 dry-run preflight / 诊断 / health check 使用：只检查已有 session，不触发副作用。
    """
    client = _patch_gate(monkeypatch, _StubState(session_ready=True))

    mt5_session_gate.probe_mt5_session_gate(
        instance_name="demo-main", auto_launch_terminal=False
    )

    assert client.inspect_calls[0]["require_terminal_process"] is True


def test_ensure_gate_raises_with_clear_message_when_initialize_fails(monkeypatch) -> None:
    """auto_launch 开启但 initialize 失败（如 path 无效）仍应 fail-fast。"""
    _patch_gate(
        monkeypatch,
        _StubState(
            session_ready=False,
            error_code="initialize_failed",
            error_message="terminal path invalid",
        ),
    )

    with pytest.raises(RuntimeError, match="initialize_failed"):
        mt5_session_gate.ensure_mt5_session_gate_or_raise(instance_name="demo-main")


def test_ensure_gate_propagates_auto_launch_flag(monkeypatch) -> None:
    """ensure_mt5_session_gate_or_raise 应把 auto_launch_terminal 传递给 probe。"""
    client = _patch_gate(monkeypatch, _StubState(session_ready=True))

    mt5_session_gate.ensure_mt5_session_gate_or_raise(
        instance_name="demo-main", auto_launch_terminal=False
    )
    assert client.inspect_calls[-1]["require_terminal_process"] is True
