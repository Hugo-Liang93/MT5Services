from __future__ import annotations

import pytest
import runpy
import sys
import types
from types import SimpleNamespace

from src.entrypoint.web import APP_TARGET, launch, resolve_runtime_target


def test_resolve_target_uses_defaults():
    target, host, port = resolve_runtime_target()

    assert target == APP_TARGET
    assert isinstance(host, str)
    assert isinstance(port, int)


def test_resolve_target_ignores_env_overrides(monkeypatch):
    monkeypatch.setenv("MT5_API_HOST", "127.0.0.1")
    monkeypatch.setenv("MT5_API_PORT", "9900")

    target, host, port = resolve_runtime_target()

    assert target == APP_TARGET
    assert host == "0.0.0.0"
    assert port == 8808


def test_launch_invokes_uvicorn(monkeypatch):
    calls = {}
    pytest.importorskip("uvicorn")

    def fake_run(target, host, port, reload, access_log):
        calls.update(
            {
                "target": target,
                "host": host,
                "port": port,
                "reload": reload,
                "access_log": access_log,
            }
        )

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setattr(
        "src.entrypoint.web.ensure_mt5_session_gate_or_raise",
        lambda instance_name=None: None,
    )
    launch()

    assert calls["target"] == APP_TARGET
    assert calls["reload"] is False


def test_launch_fails_fast_when_mt5_session_gate_fails(monkeypatch):
    pytest.importorskip("uvicorn")

    monkeypatch.setattr(
        "src.entrypoint.web.ensure_mt5_session_gate_or_raise",
        lambda instance_name=None: (_ for _ in ()).throw(RuntimeError("interactive_login_required")),
    )

    with pytest.raises(RuntimeError, match="interactive_login_required"):
        launch()


def test_main_block_invoke_launch(monkeypatch):
    import src.config as config
    import src.utils.timezone as timezone_mod

    calls = {}
    fake_uvicorn = types.SimpleNamespace(
        run=lambda target, host, port, reload, access_log: calls.update(
            target=target,
            host=host,
            port=port,
            reload=reload,
            access_log=access_log,
        )
    )

    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr(
        config,
        "get_api_config",
        lambda: SimpleNamespace(
            host="0.0.0.0",
            port=8808,
            log_format="%(message)s",
            access_log_enabled=False,
        ),
    )
    monkeypatch.setattr(
        config,
        "get_system_config",
        lambda: SimpleNamespace(
            timezone="UTC",
            log_file_enabled=False,
            log_level="INFO",
            log_file_max_mb=1,
            log_file_backup_count=1,
            log_error_file=None,
        ),
    )
    monkeypatch.setattr(timezone_mod, "configure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "src.ops.mt5_session_gate.ensure_mt5_session_gate_or_raise",
        lambda instance_name=None: None,
    )

    # 通过 run_module 执行时会触发 __main__ 分支，间接验证入口脚本行为
    # 清理已加载模块，避免运行测试时触发 runpy 的 sys.modules 缓存告警
    monkeypatch.delitem(sys.modules, "src.entrypoint.web", raising=False)
    runpy.run_module("src.entrypoint.web", run_name="__main__")

    assert calls["target"] == APP_TARGET
    assert calls["host"] == "0.0.0.0"
    assert calls["port"] == 8808
    assert calls["reload"] is False
    assert calls["access_log"] is False
