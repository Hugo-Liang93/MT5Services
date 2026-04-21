"""indicator event_loops 顶层 fail-fast 回归测试。

历史教训（2026-04-20 生产事故）：
  - `run_event_writer_loop` 在 22:44 `connection pool exhausted` 异常后静默死亡。
  - daemon thread 死亡后主进程仍跑着，但不再消费任何 confirmed bar 8.5 小时。
  - supervisor 无从感知，API 观察到的 uptime 仍正常。
修复契约：
  - 四个主循环（run_event_loop / run_intrabar_loop / run_event_writer_loop /
    run_reload_loop）必须顶层 try/except BaseException → `_on_thread_crash`。
  - 生产下 `_on_thread_crash` 调 `os._exit(1)` 让 supervisor 拉起。
  - 测试通过 monkeypatch 验证异常被捕获并转发到 crash handler。
"""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace
from typing import Any, List, Tuple

import pytest

from src.indicators.runtime import event_loops


@pytest.fixture
def captured_crashes(monkeypatch) -> List[Tuple[str, BaseException]]:
    """收集 _on_thread_crash 被触发的调用，避免真正 os._exit 杀掉 pytest。"""
    captured: List[Tuple[str, BaseException]] = []

    def _stub(thread_name: str, exc: BaseException) -> None:
        captured.append((thread_name, exc))

    monkeypatch.setattr(event_loops, "_on_thread_crash", _stub)
    return captured


def _fake_manager() -> Any:
    """最小化 manager：触发异常的 event_store + stop_event / queues。"""
    stop_event = threading.Event()
    # 让每个循环进入后立即走到会 raise 的路径
    event_store = SimpleNamespace(
        claim_next_events=lambda limit=32: (_ for _ in ()).throw(
            RuntimeError("simulated DB failure")
        )
    )
    config = SimpleNamespace(
        pipeline=SimpleNamespace(poll_interval=0.5),
        reload_interval=1.0,
        hot_reload=False,
    )
    state = SimpleNamespace(
        stop_event=stop_event,
        intrabar_queue=queue.Queue(),
        event_write_queue=queue.Queue(),
    )
    return SimpleNamespace(
        state=state,
        event_store=event_store,
        config=config,
        cleanup_old_events=lambda **_: None,
    )


def test_event_loop_crash_routes_to_crash_handler(
    captured_crashes: List[Tuple[str, BaseException]],
) -> None:
    manager = _fake_manager()
    event_loops.run_event_loop(manager)
    assert len(captured_crashes) == 1
    thread_name, exc = captured_crashes[0]
    assert thread_name == "IndicatorEventLoop"
    assert isinstance(exc, RuntimeError)
    assert "simulated DB failure" in str(exc)


def test_intrabar_loop_crash_routes_to_crash_handler(
    captured_crashes: List[Tuple[str, BaseException]],
) -> None:
    manager = _fake_manager()
    # 让 intrabar_queue.get 抛非-Empty 异常
    manager.state.intrabar_queue = SimpleNamespace(
        get=lambda timeout: (_ for _ in ()).throw(ValueError("boom"))
    )
    event_loops.run_intrabar_loop(manager)
    assert len(captured_crashes) == 1
    thread_name, exc = captured_crashes[0]
    assert thread_name == "IndicatorIntrabar"
    assert isinstance(exc, ValueError)


def test_event_writer_loop_crash_routes_to_crash_handler(
    captured_crashes: List[Tuple[str, BaseException]],
) -> None:
    manager = _fake_manager()
    manager.state.event_write_queue = SimpleNamespace(
        get=lambda timeout: (_ for _ in ()).throw(OSError("db locked"))
    )
    event_loops.run_event_writer_loop(manager)
    assert len(captured_crashes) == 1
    thread_name, exc = captured_crashes[0]
    assert thread_name == "IndicatorEventWriter"
    assert isinstance(exc, OSError)


def test_reload_loop_crash_routes_to_crash_handler(
    captured_crashes: List[Tuple[str, BaseException]],
) -> None:
    manager = _fake_manager()
    # stop_event.wait 抛 — 模拟罕见的极端 threading 错误
    manager.state = SimpleNamespace(
        stop_event=SimpleNamespace(
            wait=lambda interval: (_ for _ in ()).throw(SystemError("thread broken"))
        )
    )
    event_loops.run_reload_loop(manager)
    assert len(captured_crashes) == 1
    thread_name, exc = captured_crashes[0]
    assert thread_name == "IndicatorConfigReloader"
    assert isinstance(exc, SystemError)
