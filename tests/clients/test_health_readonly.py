"""MT5BaseClient.health() 回归测试：HTTP 路径必须只读，不触发 attempt_initialize/login。

历史教训（2026-04-20 生产事故）：
  - `/health` 路由走 market.service.health → client.health() → inspect_session_state(attempt_initialize=True, attempt_login=True)
  - MT5 terminal 假死时，每个 HTTP 请求都会同步阻塞在 mt5.initialize() 里（60s 超时）
  - 线程池被耗尽，所有 HTTP 端点超时 HTTP 000，观测性完全失效
  - 修复方式：client.health() 默认 attempt_initialize=False, attempt_login=False；
    重连职责归还给 ops tooling 和 ingestor 自身的错误恢复逻辑。
"""

from __future__ import annotations

from types import SimpleNamespace

from src.clients.base import MT5BaseClient


class _ProbeClient:
    """MT5BaseClient 子类，仅替换 inspect_session_state 以记录调用参数。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._state = SimpleNamespace(
            session_ready=True,
            login=123,
            server="S",
            terminal_name="T",
            error_message=None,
            to_dict=lambda: {"session_ready": True},
        )

    def inspect_session_state(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        return self._state


def test_health_default_is_readonly_no_mt5_reconnect() -> None:
    """client.health() 默认必须传 attempt_initialize=False, attempt_login=False。"""
    probe = _ProbeClient()
    payload = MT5BaseClient.health(probe)  # type: ignore[arg-type]
    assert probe.calls, "health() should call inspect_session_state exactly once"
    call = probe.calls[0]
    assert (
        call["attempt_initialize"] is False
    ), "HTTP 健康端点不得触发 mt5.initialize()——历史事故会导致 HTTP 线程池阻塞"
    assert call["attempt_login"] is False, "HTTP 健康端点不得触发 mt5.login()——同上"
    assert call["require_terminal_process"] is True
    assert payload["connected"] is True


def test_health_explicit_reconnect_opts_in() -> None:
    """显式传 attempt_mt5_reconnect=True 才进行重连（ops tooling 场景）。"""
    probe = _ProbeClient()
    MT5BaseClient.health(probe, attempt_mt5_reconnect=True)  # type: ignore[arg-type]
    call = probe.calls[0]
    assert call["attempt_initialize"] is True
    assert call["attempt_login"] is True
