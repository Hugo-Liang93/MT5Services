"""Tests for CorrelationAnalysisRepository — P11 Phase 3。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.persistence.repositories.correlation_repo import CorrelationAnalysisRepository

# ────────────────────────── Dummy CorrelationAnalysis ──────────────────────────


@dataclass(frozen=True)
class _Pair:
    strategy_a: str
    strategy_b: str
    correlation: float
    overlap_count: int
    agreement_rate: float


@dataclass(frozen=True)
class _Analysis:
    pairs: list
    strategy_weights: dict
    high_correlation_pairs: list
    correlation_threshold: float
    total_bars_analyzed: int


def _sample_analysis() -> _Analysis:
    pair1 = _Pair("trend_a", "trend_b", 0.85, 120, 0.92)
    pair2 = _Pair("trend_a", "mean_rev_c", 0.35, 80, 0.50)
    return _Analysis(
        pairs=[pair1, pair2],
        strategy_weights={"trend_a": 1.0, "trend_b": 0.5, "mean_rev_c": 1.0},
        high_correlation_pairs=[pair1],
        correlation_threshold=0.80,
        total_bars_analyzed=500,
    )


# ────────────────────────── Dummy Writer ──────────────────────────


class _DummyCursor:
    def __init__(self, parent: "_DummyWriter") -> None:
        self._parent = parent

    def __enter__(self) -> "_DummyCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def execute(self, sql: str, params: Any = None) -> None:
        self._parent.executes.append((sql, params))
        for pattern, result in self._parent._canned.items():
            if pattern in sql:
                self._parent._next_fetchall = result
                return
        self._parent._next_fetchall = []

    def fetchall(self) -> list:
        result = self._parent._next_fetchall
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
        self._canned: dict[str, list] = {}
        self._next_fetchall: list = []

    def _json(self, value: Any) -> Any:
        return {"_json": value}

    def connection(self) -> _DummyConnection:
        return _DummyConnection(self)

    def set_canned(self, pattern: str, rows: list) -> None:
        self._canned[pattern] = rows


# ────────────────────────── save() ──────────────────────────


def test_save_generates_analysis_id_when_not_provided() -> None:
    writer = _DummyWriter()
    repo = CorrelationAnalysisRepository(writer)

    aid = repo.save(
        _sample_analysis(),
        backtest_run_id="bt_123",
        penalty_weight=0.5,
    )

    assert aid.startswith("corr_")
    sql, params = writer.executes[0]
    assert "INSERT INTO backtest_correlation_analyses" in sql
    assert params[0] == aid
    assert params[1] == "bt_123"
    # strategies_analyzed = len(strategy_weights) = 3
    assert params[6] == 3
    # high_correlation_count = len(high_correlation_pairs) = 1
    assert params[7] == 1


def test_save_accepts_explicit_analysis_id() -> None:
    writer = _DummyWriter()
    repo = CorrelationAnalysisRepository(writer)

    aid = repo.save(
        _sample_analysis(),
        backtest_run_id="bt_abc",
        penalty_weight=0.3,
        analysis_id="corr_custom",
    )

    assert aid == "corr_custom"
    assert writer.executes[0][1][0] == "corr_custom"


def test_save_serializes_pairs_and_weights_via_json() -> None:
    writer = _DummyWriter()
    repo = CorrelationAnalysisRepository(writer)

    repo.save(
        _sample_analysis(),
        backtest_run_id="bt_x",
        penalty_weight=0.5,
    )

    params = writer.executes[0][1]
    # pairs JSONB 字段（索引 8）
    pairs_json = params[8]
    assert isinstance(pairs_json, dict)  # _json wrapper
    pairs_payload = pairs_json["_json"]
    assert len(pairs_payload) == 2
    assert pairs_payload[0]["strategy_a"] == "trend_a"
    assert pairs_payload[0]["correlation"] == 0.85

    # strategy_weights JSONB 字段（索引 10）
    weights_json = params[10]
    assert weights_json["_json"] == {
        "trend_a": 1.0,
        "trend_b": 0.5,
        "mean_rev_c": 1.0,
    }


# ────────────────────────── fetch() ──────────────────────────


def test_fetch_returns_none_when_missing() -> None:
    writer = _DummyWriter()
    repo = CorrelationAnalysisRepository(writer)

    assert repo.fetch("corr_missing") is None


def test_fetch_converts_row_to_dict() -> None:
    writer = _DummyWriter()
    now = datetime(2026, 4, 21, tzinfo=timezone.utc)
    writer.set_canned(
        "WHERE analysis_id = %s",
        [
            (
                "corr_abc",
                "bt_999",
                now,
                0.80,
                0.50,
                500,
                3,
                1,
                [{"strategy_a": "A", "strategy_b": "B"}],
                [{"strategy_a": "A", "strategy_b": "B"}],
                {"A": 1.0, "B": 0.5},
                {"all_pairs_count": 2},
            )
        ],
    )
    repo = CorrelationAnalysisRepository(writer)

    result = repo.fetch("corr_abc")

    assert result is not None
    assert result["analysis_id"] == "corr_abc"
    assert result["backtest_run_id"] == "bt_999"
    assert result["correlation_threshold"] == 0.80
    assert result["penalty_weight"] == 0.50
    assert result["total_bars_analyzed"] == 500
    assert result["strategies_analyzed"] == 3
    assert result["high_correlation_count"] == 1
    assert result["strategy_weights"] == {"A": 1.0, "B": 0.5}


def test_fetch_latest_for_run_uses_correct_sql() -> None:
    writer = _DummyWriter()
    repo = CorrelationAnalysisRepository(writer)

    repo.fetch_latest_for_run("bt_missing")

    sql, params = writer.executes[0]
    assert "ORDER BY created_at DESC" in sql
    assert params == ("bt_missing",)


def test_list_for_run_applies_pagination() -> None:
    writer = _DummyWriter()
    repo = CorrelationAnalysisRepository(writer)

    repo.list_for_run("bt_xyz", limit=5, offset=10)

    sql, params = writer.executes[0]
    assert "LIMIT %s OFFSET %s" in sql
    assert params == ("bt_xyz", 5, 10)
