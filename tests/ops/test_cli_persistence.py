"""ops/cli/_persistence.py 回归测试。

验证 CLI 入库 helper 的契约：
  - Writer 一次性打开 + 退出时关闭池（ADR-008 短生命周期约束）
  - experiment_id 透传到 result 对象
  - 单个 save 失败不影响其余；writer open 失败仅 warning 不 raise
  - _persistence 导入不触发循环依赖
"""

from __future__ import annotations

from typing import Any, List

import pytest

from src.ops.cli import _persistence


class _FakeRepo:
    """捕获 save 调用，模拟部分失败。"""

    def __init__(self, *, fail_on_run_ids: tuple[str, ...] = ()) -> None:
        self.saved: List[Any] = []
        self._fail_on = set(fail_on_run_ids)

    def save_mining_result(self, result: Any) -> None:
        if result.run_id in self._fail_on:
            raise RuntimeError("simulated save fail")
        self.saved.append(result)

    def save_result(self, result: Any) -> None:
        if result.run_id in self._fail_on:
            raise RuntimeError("simulated save fail")
        self.saved.append(result)


class _FakeWriter:
    """最小 writer，暴露 research_repo / backtest_repo + 可关闭 pool。"""

    def __init__(self, repo: _FakeRepo) -> None:
        self.research_repo = repo
        self.backtest_repo = repo
        self._pool = _FakePool()


class _FakePool:
    def __init__(self) -> None:
        self.closed = False

    def closeall(self) -> None:
        self.closed = True


class _MiningResult:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.experiment_id: str | None = None


class _BacktestResult:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.experiment_id: str | None = None


@pytest.fixture
def patched_writer(monkeypatch):
    """让 _writer_scope 返回 _FakeWriter，避免真连 pg。"""

    repo = _FakeRepo()
    writer = _FakeWriter(repo)

    from contextlib import contextmanager

    @contextmanager
    def _stub_scope(env: str):
        yield writer

    monkeypatch.setattr(_persistence, "_writer_scope", _stub_scope)
    return writer, repo


def test_persist_mining_results_saves_each_and_sets_experiment(patched_writer):
    writer, repo = patched_writer
    r1 = _MiningResult("mine_1")
    r2 = _MiningResult("mine_2")
    saved = _persistence.persist_mining_results(
        {"H1": r1, "M30": r2}, environment="live", experiment_id="exp_42"
    )
    assert saved == 2
    assert [r.run_id for r in repo.saved] == ["mine_1", "mine_2"]
    assert r1.experiment_id == "exp_42"
    assert r2.experiment_id == "exp_42"


def test_persist_mining_results_partial_failure_continues(monkeypatch):
    repo = _FakeRepo(fail_on_run_ids=("mine_bad",))
    writer = _FakeWriter(repo)
    from contextlib import contextmanager

    @contextmanager
    def _scope(env: str):
        yield writer

    monkeypatch.setattr(_persistence, "_writer_scope", _scope)

    r1 = _MiningResult("mine_bad")
    r2 = _MiningResult("mine_ok")
    saved = _persistence.persist_mining_results(
        {"H1": r1, "M30": r2}, environment="live"
    )
    assert saved == 1
    assert [r.run_id for r in repo.saved] == ["mine_ok"]


def test_persist_mining_results_empty_returns_zero():
    assert _persistence.persist_mining_results({}, environment="live") == 0


def test_persist_backtest_result_single(patched_writer):
    writer, repo = patched_writer
    r = _BacktestResult("bt_1")
    ok = _persistence.persist_backtest_result(
        r, environment="demo", experiment_id="exp_1"
    )
    assert ok is True
    assert r.experiment_id == "exp_1"
    assert repo.saved[0].run_id == "bt_1"


def test_persist_backtest_results_many(patched_writer):
    writer, repo = patched_writer
    rs = [_BacktestResult(f"bt_{i}") for i in range(3)]
    saved = _persistence.persist_backtest_results_many(
        rs, environment="live", experiment_id="exp_batch"
    )
    assert saved == 3
    assert all(r.experiment_id == "exp_batch" for r in rs)


def test_writer_scope_failure_is_warning_not_raise(monkeypatch, caplog):
    """打开 writer 失败时 helper 吞异常、仅 warning，不阻塞 CLI 退出。"""
    from contextlib import contextmanager

    @contextmanager
    def _broken_scope(env: str):
        raise ConnectionError("pg down")
        yield  # pragma: no cover

    monkeypatch.setattr(_persistence, "_writer_scope", _broken_scope)
    import logging

    with caplog.at_level(logging.WARNING, logger="src.ops.cli._persistence"):
        saved = _persistence.persist_mining_results(
            {"H1": _MiningResult("x")}, environment="live"
        )
    assert saved == 0
    assert any(
        "Failed to open writer" in r.message for r in caplog.records
    ), "pg down 必须 warning，不能 raise 让 CLI 崩"


def test_add_persist_arguments_registers_expected_flags():
    """add_persist_arguments 注册 --persist / --experiment 两个 flag。"""
    import argparse

    parser = argparse.ArgumentParser()
    _persistence.add_persist_arguments(parser)
    args = parser.parse_args([])
    assert args.persist is False
    assert args.experiment is None

    args2 = parser.parse_args(["--persist", "--experiment", "exp_99"])
    assert args2.persist is True
    assert args2.experiment == "exp_99"
