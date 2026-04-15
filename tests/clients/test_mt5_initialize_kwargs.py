"""MT5BaseClient._initialize_kwargs / _login_kwargs 职责边界回归测试。

设计原则：
- _initialize_kwargs 必须返回完整 session 建立参数（path + login + password + server + timeout）
  让 mt5.initialize() 一次性完成"拉起 terminal + 自动登录 + 建立 IPC"，
  不允许只返回 path（会导致 terminal 弹登录界面等人工，IPC 永远 timeout）。
- _login_kwargs 仅作为账户切换备用接口（mt5.login() 调用），不参与 initialize。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.clients.base import MT5BaseClient


def _make_settings(*, login=12345, password="pwd", server="Demo-Server",
                   path="C:/MT5/terminal64.exe") -> SimpleNamespace:
    return SimpleNamespace(
        mt5_login=login,
        mt5_password=password,
        mt5_server=server,
        mt5_path=path,
        instance_name="test",
        account_alias="test",
        account_label=None,
        timezone="UTC",
        server_time_offset_hours=None,
    )


def test_initialize_kwargs_includes_full_credentials() -> None:
    """initialize_kwargs 必须带 path + login + password + server + timeout。

    缺任一项都会导致 mt5.initialize 退化为"只拉起 terminal"，
    用户面前会出现登录窗口、IPC timeout、启动失败。
    """
    client = MT5BaseClient(settings=_make_settings())
    kwargs = client._initialize_kwargs()

    assert kwargs["path"] == "C:/MT5/terminal64.exe"
    assert kwargs["login"] == 12345
    assert kwargs["password"] == "pwd"
    assert kwargs["server"] == "Demo-Server"
    # timeout 单位毫秒，覆盖 terminal 冷启动 + 登录握手
    assert kwargs["timeout"] >= 30000


def test_initialize_kwargs_login_coerced_to_int() -> None:
    """login 字段必须强制 int — MT5 库要求严格 int 类型。"""
    client = MT5BaseClient(settings=_make_settings(login="98765"))
    kwargs = client._initialize_kwargs()
    assert kwargs["login"] == 98765
    assert isinstance(kwargs["login"], int)


def test_initialize_kwargs_omits_missing_fields() -> None:
    """settings 字段为空时，对应 kwarg 不出现（让 mt5 库用自身默认）。"""
    settings = _make_settings(login=None, password="", server="", path="")
    client = MT5BaseClient(settings=settings)
    kwargs = client._initialize_kwargs()

    assert "login" not in kwargs
    assert "password" not in kwargs
    assert "server" not in kwargs
    assert "path" not in kwargs
    # timeout 始终设置（不依赖 settings）
    assert "timeout" in kwargs


def test_login_kwargs_remains_for_account_switch() -> None:
    """_login_kwargs 保留作为账户切换接口（不带 timeout，不带 path）。"""
    client = MT5BaseClient(settings=_make_settings())
    kwargs = client._login_kwargs()

    assert kwargs["login"] == 12345
    assert kwargs["password"] == "pwd"
    assert kwargs["server"] == "Demo-Server"
    # login 接口不接受 path / timeout — 这些是 initialize 专属
    assert "path" not in kwargs
    assert "timeout" not in kwargs
