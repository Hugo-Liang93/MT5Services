"""Tests for WalkForwardRepository — P11 Phase 2。

用 DummyWriter + DummyCursor 断言传给 DB 的 SQL 和参数，不启动真实 pg 连接。
覆盖：
- save() 单事务写 runs + 所有 windows
- save() 将 inf 过拟合比率转为 None（NUMERIC 列不能接受 inf）
- save_failed() 记录失败快照
- fetch() 组装 run + windows bundle
- fetch_latest_by_backtest_run() 未命中返 None
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from src.persistence.repositories.walk_forward_repo import WalkForwardRepository

# ────────────────────────── Dummy WalkForwardResult ──────────────────────────


@dataclass
class _DummyMetrics:
    total_trades: int = 10
    winning_trades: int = 6
    losing_trades: int = 4
    win_rate: float = 0.6
    expectancy: float = 50.0
    profit_factor: float = 1.8
    sharpe_ratio: float = 1.2
    sortino_ratio: float = 1.5
    max_drawdown: float = 100.0
    max_drawdown_duration: int = 5
    avg_win: float = 80.0
    avg_loss: float = -40.0
    avg_bars_held: float = 10.0
    total_pnl: float = 400.0
    total_pnl_pct: float = 0.04
    calmar_ratio: float = 2.0
    max_consecutive_wins: int = 3
    max_consecutive_losses: int = 2


@dataclass
class _DummyConfig:
    symbol: str = "XAUUSD"
    timeframe: str = "H1"


@dataclass
class _DummyWFConfig:
    total_start_time: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)
    total_end_time: datetime = datetime(2026, 4, 1, tzinfo=timezone.utc)
    train_ratio: float = 0.7
    n_splits: int = 3
    anchored: bool = False
    optimization_metric: str = "sharpe_ratio"
    base_config: _DummyConfig = field(default_factory=_DummyConfig)


@dataclass
class _DummyBacktestResult:
    metrics: _DummyMetrics


@dataclass
class _DummySplit:
    split_index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    best_params: dict
    in_sample_result: _DummyBacktestResult
    out_of_sample_result: _DummyBacktestResult


@dataclass
class _DummyWFResult:
    splits: list
    aggregate_metrics: _DummyMetrics = field(default_factory=_DummyMetrics)
    overfitting_ratio: float = 1.2
    consistency_rate: float = 0.67
    config: _DummyWFConfig = field(default_factory=_DummyWFConfig)


def _make_wf_result(*, overfitting_ratio: float = 1.2) -> _DummyWFResult:
    def _split(i: int) -> _DummySplit:
        return _DummySplit(
            split_index=i,
            train_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            train_end=datetime(2026, 2, 1, tzinfo=timezone.utc),
            test_start=datetime(2026, 2, 1, tzinfo=timezone.utc),
            test_end=datetime(2026, 3, 1, tzinfo=timezone.utc),
            best_params={"p": i},
            in_sample_result=_DummyBacktestResult(_DummyMetrics(sharpe_ratio=1.5)),
            out_of_sample_result=_DummyBacktestResult(_DummyMetrics(sharpe_ratio=1.0)),
        )

    return _DummyWFResult(
        splits=[_split(0), _split(1), _split(2)],
        overfitting_ratio=overfitting_ratio,
    )


# ────────────────────────── Dummy Writer ──────────────────────────


class _DummyCursor:
    def __init__(self, parent: "_DummyWriter") -> None:
        self._parent = parent

    def __enter__(self) -> "_DummyCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self._parent.executes.append((sql, params))
        self._parent._next_fetchall = self._parent._canned.get(id(sql), [])
        # 根据 SQL 内容匹配 canned result
        for pattern, result in self._parent._canned_by_pattern.items():
            if pattern in sql:
                self._parent._next_fetchall = result
                return

    def fetchall(self) -> list:
        result = getattr(self._parent, "_next_fetchall", [])
        self._parent._next_fetchall = []
        return result


class _DummyConnection:
    def __init__(self, parent: "_DummyWriter") -> None:
        self._parent = parent

    def __enter__(self) -> "_DummyConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def cursor(self) -> _DummyCursor:
        return _DummyCursor(self._parent)


class _DummyWriter:
    def __init__(self) -> None:
        self.executes: list[tuple[str, Any]] = []
        self._canned: dict[int, list] = {}
        self._canned_by_pattern: dict[str, list] = {}
        self._next_fetchall: list = []

    def _json(self, value: Any) -> Any:
        return {"_json": value}

    def connection(self) -> _DummyConnection:
        return _DummyConnection(self)

    def set_canned(self, pattern: str, rows: list) -> None:
        """当执行的 SQL 包含 `pattern` 时，下次 fetchall 返回 rows。"""
        self._canned_by_pattern[pattern] = rows


# ────────────────────────── save() ──────────────────────────


def test_save_writes_run_and_all_windows_in_same_connection() -> None:
    writer = _DummyWriter()
    repo = WalkForwardRepository(writer)
    result = _make_wf_result()

    repo.save(
        result,
        wf_run_id="wf_abc",
        backtest_run_id="bt_123",
        experiment_id="exp_1",
    )

    # 1 run INSERT + 3 window INSERT
    assert len(writer.executes) == 4
    run_sql, run_params = writer.executes[0]
    assert "INSERT INTO backtest_walk_forward_runs" in run_sql
    assert run_params[0] == "wf_abc"
    assert run_params[1] == "bt_123"
    assert run_params[12] == 3  # n_splits
    assert run_params[16] == "exp_1"  # experiment_id

    for i in range(3):
        win_sql, win_params = writer.executes[1 + i]
        assert "INSERT INTO backtest_walk_forward_windows" in win_sql
        assert win_params[0] == "wf_abc"
        assert win_params[1] == i  # split_index


def test_save_converts_inf_overfitting_to_none() -> None:
    """overfitting_ratio=inf 不能写 NUMERIC 列，必须转 None。"""
    writer = _DummyWriter()
    repo = WalkForwardRepository(writer)
    result = _make_wf_result(overfitting_ratio=float("inf"))

    repo.save(result, wf_run_id="wf_inf")

    run_params = writer.executes[0][1]
    # overfitting_ratio 是 run_row 索引 6
    assert run_params[6] is None


def test_save_writes_window_label_and_metrics() -> None:
    writer = _DummyWriter()
    repo = WalkForwardRepository(writer)
    result = _make_wf_result()

    repo.save(result, wf_run_id="wf_xyz")

    # 第二条（split_index=0）window
    _, win_params = writer.executes[1]
    # window_label 是 'train_start_date→test_end_date' 格式
    assert "2026-01-01" in win_params[2]
    assert "2026-03-01" in win_params[2]
    # is_sharpe / oos_sharpe 独立字段写入
    assert win_params[12] == 1.5  # is_sharpe
    assert win_params[13] == 1.0  # oos_sharpe


def test_save_failed_records_error() -> None:
    writer = _DummyWriter()
    repo = WalkForwardRepository(writer)

    repo.save_failed(wf_run_id="wf_fail", error="boom")

    assert len(writer.executes) == 1
    sql, params = writer.executes[0]
    assert "INSERT INTO backtest_walk_forward_runs" in sql
    assert params[0] == "wf_fail"
    assert params[13] == "failed"  # status
    assert params[15] == "boom"  # error


def test_save_failed_truncates_long_error() -> None:
    writer = _DummyWriter()
    repo = WalkForwardRepository(writer)
    long_error = "x" * 1000

    repo.save_failed(wf_run_id="wf_fail", error=long_error)

    params = writer.executes[0][1]
    assert len(params[15]) <= 500


# ────────────────────────── fetch() ──────────────────────────


def test_fetch_returns_none_when_wf_run_id_missing() -> None:
    writer = _DummyWriter()
    repo = WalkForwardRepository(writer)

    result = repo.fetch("wf_missing")
    assert result is None


def test_fetch_assembles_run_and_windows() -> None:
    writer = _DummyWriter()
    now = datetime(2026, 4, 21, tzinfo=timezone.utc)
    # FETCH_RUN_SQL 命中：返回 1 行
    writer.set_canned(
        "FROM backtest_walk_forward_runs\nWHERE wf_run_id",
        [
            (
                "wf_abc",
                "bt_123",
                now,
                now,
                {"symbol": "XAUUSD"},
                {"sharpe_ratio": 1.2},
                1.1,
                0.67,
                1.5,
                0.6,
                30,
                300.0,
                3,
                "completed",
                1200,
                None,
                "exp_1",
            )
        ],
    )
    # FETCH_WINDOWS_SQL 命中：返回 2 条窗口
    writer.set_canned(
        "FROM backtest_walk_forward_windows",
        [
            (
                0,
                "w1",
                now,
                now,
                now,
                now,
                {"p": 0},
                {"sharpe_ratio": 1.5},
                {"sharpe_ratio": 1.0},
                50.0,
                30.0,
                1.5,
                1.0,
                0.6,
                0.5,
                20.0,
                10,
            ),
            (
                1,
                "w2",
                now,
                now,
                now,
                now,
                {"p": 1},
                {"sharpe_ratio": 1.4},
                {"sharpe_ratio": 0.9},
                45.0,
                25.0,
                1.4,
                0.9,
                0.6,
                0.45,
                25.0,
                8,
            ),
        ],
    )
    repo = WalkForwardRepository(writer)

    bundle = repo.fetch("wf_abc")

    assert bundle is not None
    assert bundle["run"]["wf_run_id"] == "wf_abc"
    assert bundle["run"]["backtest_run_id"] == "bt_123"
    assert bundle["run"]["overfitting_ratio"] == 1.1
    assert bundle["run"]["consistency_rate"] == 0.67
    assert bundle["run"]["experiment_id"] == "exp_1"
    assert len(bundle["windows"]) == 2
    assert bundle["windows"][0]["split_index"] == 0
    assert bundle["windows"][0]["oos_sharpe"] == 1.0
    assert bundle["windows"][1]["split_index"] == 1


def test_fetch_latest_by_backtest_run_returns_none_when_empty() -> None:
    writer = _DummyWriter()
    repo = WalkForwardRepository(writer)

    result = repo.fetch_latest_by_backtest_run("bt_missing")

    assert result is None


# ────────────────────────── list_runs() ──────────────────────────


def test_list_runs_applies_status_filter() -> None:
    writer = _DummyWriter()
    writer.set_canned("FROM backtest_walk_forward_runs", [])
    repo = WalkForwardRepository(writer)

    repo.list_runs(status="completed", limit=10, offset=0)

    sql, params = writer.executes[-1]
    assert "WHERE status = %s" in sql
    assert params == ("completed", 10, 0)


def test_list_runs_no_filter_omits_where() -> None:
    writer = _DummyWriter()
    writer.set_canned("FROM backtest_walk_forward_runs", [])
    repo = WalkForwardRepository(writer)

    repo.list_runs(limit=5, offset=0)

    sql, params = writer.executes[-1]
    assert "WHERE" not in sql
    assert params == (5, 0)
