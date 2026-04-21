"""import_historical_mining CLI 回归测试。

验证：
  - JSON → row 转换契约（run_id / tf / start_time / end_time / n_bars / data_summary /
    top_findings / full_result JSONB）
  - dry-run 不写 DB，--execute 才 batch insert
  - 缺 run_id / tf 的 result 被 skip
  - 实际文件 → rows 的端到端转换
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.ops.cli import import_historical_mining as imp


class _FakeWriter:
    def __init__(self) -> None:
        self.batches: List[tuple[str, List[tuple]]] = []

    def _json(self, value: Any) -> Any:
        # 保留 dict/list 原样，方便断言
        return value

    def _batch(self, sql: str, rows: List[tuple]) -> None:
        self.batches.append((sql, rows))


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _sample_payload() -> Dict[str, Any]:
    return {
        "symbol": "XAUUSD",
        "results": [
            {
                "tf": "M15",
                "start": "2025-10-01",
                "end": "2026-03-30",
                "run_id": "mine_abc_M15",
                "data_summary": {
                    "n_bars": 12000,
                    "train_bars": 9000,
                    "test_bars": 3000,
                },
                "predictive_power": {"total_tested": 800, "significant": 13},
                "top_findings": [{"indicator": "x", "ic": 0.12}],
            },
            {
                "tf": "H1",
                "start": "2025-10-01T00:00:00",
                "end": "2026-03-30T00:00:00",
                "run_id": "mine_abc_H1",
                "data_summary": {"n_bars": 3000},
                "top_findings": [],
            },
        ],
    }


def test_parse_iso_handles_date_only_and_timezone() -> None:
    assert imp._parse_iso(None) is None
    assert imp._parse_iso("") is None
    d = imp._parse_iso("2025-10-01")
    assert d is not None
    assert d.tzinfo is not None
    assert d.year == 2025 and d.month == 10

    d2 = imp._parse_iso("2025-10-01T12:34:56Z")
    assert d2 is not None
    assert d2.hour == 12


def test_build_row_extracts_expected_fields() -> None:
    writer = _FakeWriter()
    tf_result = _sample_payload()["results"][0]
    row = imp._build_row(
        tf_result=tf_result,
        symbol="XAUUSD",
        experiment_id="historical_20260415",
        writer=writer,
    )
    assert row is not None
    (
        run_id,
        experiment_id,
        symbol,
        tf,
        start_time,
        end_time,
        n_bars,
        status,
        data_summary,
        top_findings,
        full_result,
    ) = row
    assert run_id == "mine_abc_M15"
    assert experiment_id == "historical_20260415"
    assert symbol == "XAUUSD"
    assert tf == "M15"
    assert n_bars == 12000
    assert status == "completed"
    assert data_summary == {"n_bars": 12000, "train_bars": 9000, "test_bars": 3000}
    assert top_findings == [{"indicator": "x", "ic": 0.12}]
    # full_result 包含原 tf_result 所有字段
    assert full_result["run_id"] == "mine_abc_M15"
    assert full_result["predictive_power"]["significant"] == 13
    assert start_time is not None and start_time.year == 2025
    assert end_time is not None and end_time.year == 2026


def test_build_row_skips_when_missing_run_id_or_tf() -> None:
    writer = _FakeWriter()
    assert (
        imp._build_row(
            tf_result={"tf": "M15"}, symbol="x", experiment_id=None, writer=writer
        )
        is None
    )
    assert (
        imp._build_row(
            tf_result={"run_id": "x"},
            symbol="x",
            experiment_id=None,
            writer=writer,
        )
        is None
    )


def test_import_file_dry_run_does_not_write(tmp_path: Path) -> None:
    f = tmp_path / "mining_test.json"
    _write_json(f, _sample_payload())
    writer = _FakeWriter()
    stats = imp._import_file(
        path=f, writer=writer, experiment_prefix="historical", dry_run=True
    )
    assert stats["results"] == 2
    assert stats["rows_built"] == 2
    assert stats["rows_written"] == 0  # dry-run 不写
    assert writer.batches == []


def test_import_file_execute_writes_rows(tmp_path: Path) -> None:
    f = tmp_path / "mining_test.json"
    _write_json(f, _sample_payload())
    writer = _FakeWriter()
    stats = imp._import_file(
        path=f, writer=writer, experiment_prefix="historical", dry_run=False
    )
    assert stats["rows_written"] == 2
    assert len(writer.batches) == 1
    sql, rows = writer.batches[0]
    assert "INSERT INTO research_mining_runs" in sql
    assert len(rows) == 2
    # 确认 experiment_id 有 historical_YYYYMMDD 格式
    exp_ids = {r[1] for r in rows}
    assert len(exp_ids) == 1
    (single_exp,) = exp_ids
    assert single_exp.startswith("historical_")
    assert len(single_exp.split("_")[1]) == 8  # YYYYMMDD


def test_import_file_skips_malformed_entries(tmp_path: Path) -> None:
    f = tmp_path / "mining_bad.json"
    _write_json(
        f,
        {
            "symbol": "XAUUSD",
            "results": [
                {"tf": "M15", "run_id": "ok_1", "data_summary": {"n_bars": 100}},
                {"tf": "M30"},  # 缺 run_id
                "not a dict",  # 非 dict
                {"run_id": "missing_tf"},  # 缺 tf
            ],
        },
    )
    writer = _FakeWriter()
    stats = imp._import_file(
        path=f, writer=writer, experiment_prefix="historical", dry_run=False
    )
    assert stats["results"] == 4
    assert stats["rows_built"] == 1
    assert stats["skipped"] == 3
    assert stats["rows_written"] == 1


def test_import_file_empty_results_handled(tmp_path: Path) -> None:
    f = tmp_path / "mining_empty.json"
    _write_json(f, {"symbol": "XAUUSD", "results": []})
    writer = _FakeWriter()
    stats = imp._import_file(
        path=f, writer=writer, experiment_prefix="historical", dry_run=False
    )
    assert stats["rows_built"] == 0
    assert writer.batches == []


def test_import_file_handles_missing_results_key(tmp_path: Path) -> None:
    f = tmp_path / "mining_noresults.json"
    _write_json(f, {"symbol": "XAUUSD"})
    writer = _FakeWriter()
    stats = imp._import_file(
        path=f, writer=writer, experiment_prefix="historical", dry_run=False
    )
    assert stats["results"] == 0
    assert stats["rows_built"] == 0
