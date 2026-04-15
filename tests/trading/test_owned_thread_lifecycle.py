"""OwnedThreadLifecycle 独立单元测试（ADR-005 安全契约守卫）。

ADR-005 关键约束：stop(timeout) 后必须保留仍存活线程引用，防止双线程消费。
本文件守住这个 contract，以及 is_running / ensure_running / wait_previous 的
正常路径 + 边界。
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from src.trading.runtime.lifecycle import OwnedThreadLifecycle


# ── 测试辅助 ────────────────────────────────────────────────────────────


def _make_owner() -> SimpleNamespace:
    """模拟"持有线程引用属性"的 owner 对象（如 TradeExecutor 持有 _exec_thread）。"""
    return SimpleNamespace(_worker_thread=None)


def _make_lifecycle(owner=None) -> OwnedThreadLifecycle:
    return OwnedThreadLifecycle(
        owner=owner or _make_owner(),
        thread_attr="_worker_thread",
        label="TestWorker",
    )


def _noop_loop(stop_event: threading.Event) -> None:
    """合作式 worker：set 后立即退出。"""
    while not stop_event.wait(0.01):
        pass


def _stuck_loop(stop_event: threading.Event) -> None:
    """模拟卡住的 worker：忽略 stop_event 一段时间。

    用于验证 stop(timeout) 在 worker 不响应时的"保留引用"行为。
    """
    time.sleep(2.0)


# ── is_running ────────────────────────────────────────────────────────────


def test_is_running_false_when_no_thread() -> None:
    """从未启动 → 不在跑。"""
    lifecycle = _make_lifecycle()
    assert lifecycle.is_running() is False


def test_is_running_true_when_thread_alive() -> None:
    """线程跑着 → True。"""
    owner = _make_owner()
    lifecycle = _make_lifecycle(owner)
    stop_event = threading.Event()
    lifecycle.ensure_running(
        lambda: threading.Thread(target=_noop_loop, args=(stop_event,), daemon=True)
    )
    try:
        assert lifecycle.is_running() is True
    finally:
        lifecycle.stop(stop_event, timeout=2.0)


def test_is_running_false_after_thread_exits() -> None:
    """线程已退出 → False（即使引用还挂着）。"""
    owner = _make_owner()
    lifecycle = _make_lifecycle(owner)
    stop_event = threading.Event()
    lifecycle.ensure_running(
        lambda: threading.Thread(target=_noop_loop, args=(stop_event,), daemon=True)
    )
    stop_event.set()
    # 等线程自然退出
    owner._worker_thread.join(timeout=2.0)
    assert lifecycle.is_running() is False


# ── ensure_running ───────────────────────────────────────────────────────


def test_ensure_running_starts_thread_when_idle() -> None:
    """无线程时 → 调 factory 创建 + start，返回 True。"""
    owner = _make_owner()
    lifecycle = _make_lifecycle(owner)
    stop_event = threading.Event()
    started = lifecycle.ensure_running(
        lambda: threading.Thread(target=_noop_loop, args=(stop_event,), daemon=True)
    )
    try:
        assert started is True
        assert owner._worker_thread is not None
        assert owner._worker_thread.is_alive()
    finally:
        lifecycle.stop(stop_event, timeout=2.0)


def test_ensure_running_skips_when_already_alive() -> None:
    """线程在跑 → 不调 factory，返回 False（防双线程消费）。"""
    owner = _make_owner()
    lifecycle = _make_lifecycle(owner)
    stop_event = threading.Event()
    factory_calls = []

    def factory() -> threading.Thread:
        factory_calls.append(1)
        return threading.Thread(target=_noop_loop, args=(stop_event,), daemon=True)

    lifecycle.ensure_running(factory)
    try:
        # 第二次调用 → 跳过
        result = lifecycle.ensure_running(factory)
        assert result is False
        assert len(factory_calls) == 1  # factory 只被调一次
    finally:
        lifecycle.stop(stop_event, timeout=2.0)


def test_ensure_running_recreates_after_thread_exits() -> None:
    """线程已死 → ensure_running 应该再创建（不卡在僵尸引用）。"""
    owner = _make_owner()
    lifecycle = _make_lifecycle(owner)
    stop1 = threading.Event()
    lifecycle.ensure_running(
        lambda: threading.Thread(target=_noop_loop, args=(stop1,), daemon=True)
    )
    first_thread = owner._worker_thread
    stop1.set()
    first_thread.join(timeout=2.0)
    assert not first_thread.is_alive()

    # 旧线程已死，ensure_running 应能再起新线程
    stop2 = threading.Event()
    started = lifecycle.ensure_running(
        lambda: threading.Thread(target=_noop_loop, args=(stop2,), daemon=True)
    )
    try:
        assert started is True
        assert owner._worker_thread is not first_thread  # 新线程
        assert owner._worker_thread.is_alive()
    finally:
        lifecycle.stop(stop2, timeout=2.0)


# ── wait_previous ─────────────────────────────────────────────────────────


def test_wait_previous_returns_true_when_no_thread() -> None:
    """无线程引用 → True 直接返回。"""
    lifecycle = _make_lifecycle()
    assert lifecycle.wait_previous(timeout=0.5) is True


def test_wait_previous_clears_dead_thread_reference() -> None:
    """线程已退出 → True 并清引用。"""
    owner = _make_owner()
    lifecycle = _make_lifecycle(owner)
    stop_event = threading.Event()
    lifecycle.ensure_running(
        lambda: threading.Thread(target=_noop_loop, args=(stop_event,), daemon=True)
    )
    stop_event.set()
    owner._worker_thread.join(timeout=2.0)
    # 此时 thread 已死但引用还在
    assert owner._worker_thread is not None
    assert lifecycle.wait_previous(timeout=0.5) is True
    # 引用被清
    assert owner._worker_thread is None


def test_wait_previous_returns_false_and_keeps_reference_on_timeout() -> None:
    """ADR-005 关键 contract：thread 仍 alive 且超时 → 返 False 并**保留引用**，
    防止后续 ensure_running 创建第二个线程导致双线程消费。"""
    owner = _make_owner()
    lifecycle = _make_lifecycle(owner)
    # 启动一个会卡 2s 的线程
    stuck_thread = threading.Thread(target=_stuck_loop, args=(threading.Event(),), daemon=True)
    stuck_thread.start()
    owner._worker_thread = stuck_thread
    try:
        # timeout 0.1s 远小于 stuck_loop 的 2s
        result = lifecycle.wait_previous(timeout=0.1)
        assert result is False
        # 关键：引用必须保留，不能被清成 None
        assert owner._worker_thread is stuck_thread
        assert owner._worker_thread.is_alive()
    finally:
        # 等 stuck thread 自然退出避免污染后续测试
        stuck_thread.join(timeout=3.0)


# ── stop ─────────────────────────────────────────────────────────────────


def test_stop_sets_event_and_waits_for_clean_exit() -> None:
    """正常路径：stop 置位 event + worker 退出 + 清引用。"""
    owner = _make_owner()
    lifecycle = _make_lifecycle(owner)
    stop_event = threading.Event()
    lifecycle.ensure_running(
        lambda: threading.Thread(target=_noop_loop, args=(stop_event,), daemon=True)
    )
    stopped = lifecycle.stop(stop_event, timeout=2.0)
    assert stopped is True
    assert stop_event.is_set()
    assert owner._worker_thread is None  # 干净退出后引用被清


def test_stop_returns_false_and_preserves_thread_when_worker_does_not_respond() -> None:
    """ADR-005 contract：stop 超时（worker 不响应 stop_event） → 返 False，
    保留 thread 引用让下一次 start() 通过 ensure_running 等待僵尸退出。"""
    owner = _make_owner()
    lifecycle = _make_lifecycle(owner)
    # _stuck_loop 完全忽略 stop_event，强制 timeout
    own_event = threading.Event()
    stuck = threading.Thread(target=_stuck_loop, args=(own_event,), daemon=True)
    stuck.start()
    owner._worker_thread = stuck

    stopped = lifecycle.stop(own_event, timeout=0.1)
    try:
        assert stopped is False
        # 引用保留（不清空）
        assert owner._worker_thread is stuck
        assert owner._worker_thread.is_alive()
    finally:
        stuck.join(timeout=3.0)


def test_stop_is_idempotent_when_thread_already_dead() -> None:
    """已停止的线程再调 stop → 安全，不抛。"""
    owner = _make_owner()
    lifecycle = _make_lifecycle(owner)
    stop_event = threading.Event()
    lifecycle.ensure_running(
        lambda: threading.Thread(target=_noop_loop, args=(stop_event,), daemon=True)
    )
    lifecycle.stop(stop_event, timeout=2.0)
    # 第二次 stop —— 应该安全直接返回 True
    second_stop_event = threading.Event()
    second_stop = lifecycle.stop(second_stop_event, timeout=0.1)
    assert second_stop is True
