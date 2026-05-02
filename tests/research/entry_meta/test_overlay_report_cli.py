from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.ops.cli.entry_meta_overlay_report import build_report_from_paths


def test_entry_meta_overlay_report_help_exposes_required_options() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "src.ops.cli.entry_meta_overlay_report", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--baseline" in completed.stdout
    assert "--shadow" in completed.stdout
    assert "--filters" in completed.stdout
    assert "--json-output" in completed.stdout


def test_build_report_from_paths_reads_backtest_runner_raw_results_for_attribution(
    tmp_path: Path,
) -> None:
    baseline_path = tmp_path / "baseline.json"
    shadow_path = tmp_path / "shadow.json"
    filter_path = tmp_path / "filter.json"
    baseline_payload = {
        "results": [
            {
                "tf": "H1",
                "metrics": {
                    "trades": 12,
                    "pnl": 120.0,
                    "pf": 1.2,
                    "expectancy": 10.0,
                    "max_dd": 8.0,
                },
            }
        ],
        "raw_results": [
            {
                "trades": [
                    {
                        "entry_time": "2026-01-01T00:00:00+00:00",
                        "exit_time": "2026-01-01T03:00:00+00:00",
                        "strategy": "breakout",
                        "direction": "buy",
                        "pnl": 42.5,
                        "exit_reason": "take_profit",
                    }
                ]
            }
        ],
    }
    shadow_payload = {
        "results": [
            {
                "tf": "H1",
                "metrics": {
                    "trades": 12,
                    "pnl": 120.0,
                    "pf": 1.2,
                    "expectancy": 10.0,
                    "max_dd": 8.0,
                },
            }
        ]
    }
    filter_payload = {
        "results": [
            {
                "tf": "H1",
                "entry_meta_threshold": 0.65,
                "metrics": {
                    "trades": 11,
                    "pnl": 128.0,
                    "pf": 1.3,
                    "expectancy": 11.64,
                    "max_dd": 7.5,
                },
                "execution_summary": {
                    "blocked_entry_events": [
                        {
                            "source": "entry_meta_overlay",
                            "bar_time": "2026-01-01T00:00:00Z",
                            "strategy": "breakout",
                            "direction": "buy",
                            "take_entry_prob": 0.45,
                            "block_entry_prob": 0.55,
                            "threshold": 0.65,
                        }
                    ]
                },
            }
        ],
        "raw_results": [{"trades": []}],
    }
    baseline_path.write_text(
        json.dumps(baseline_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    shadow_path.write_text(
        json.dumps(shadow_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    filter_path.write_text(
        json.dumps(filter_payload, ensure_ascii=False),
        encoding="utf-8",
    )

    report = build_report_from_paths(
        baseline_path,
        shadow_path,
        [filter_path],
        min_trades=10,
    )
    payload = report.to_dict()

    assert payload["shadow_check"]["status"] == "passed"
    attribution = payload["filter_diagnostics"][0]["blocked_trade_attribution"]
    assert attribution["matched_trades"] == 1
    assert attribution["matched_pnl"] == 42.5
    assert attribution["attributed_events"][0]["matched_trade"]["exit_reason"] == (
        "take_profit"
    )


def test_build_report_from_paths_expands_all_filter_results(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.json"
    filter_path = tmp_path / "filters.json"
    baseline_path.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "metrics": {
                            "trades": 12,
                            "pnl": 120.0,
                            "pf": 1.2,
                            "expectancy": 10.0,
                            "max_dd": 8.0,
                        },
                    }
                ],
                "raw_results": [{"trades": []}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    filter_path.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "entry_meta_threshold": 0.60,
                        "metrics": {
                            "trades": 11,
                            "pnl": 125.0,
                            "pf": 1.25,
                            "expectancy": 11.36,
                            "max_dd": 8.0,
                        },
                    },
                    {
                        "entry_meta_threshold": 0.70,
                        "metrics": {
                            "trades": 10,
                            "pnl": 130.0,
                            "pf": 1.30,
                            "expectancy": 13.0,
                            "max_dd": 7.5,
                        },
                    },
                ],
                "raw_results": [{"trades": []}, {"trades": []}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = build_report_from_paths(
        baseline_path,
        shadow_path=None,
        filter_paths=[filter_path],
        min_trades=10,
    ).to_dict()

    threshold_results = payload["threshold_report"]["threshold_results"]
    assert [item["threshold"] for item in threshold_results] == [0.60, 0.70]
    assert len(threshold_results) == 2
    assert len(payload["filter_diagnostics"]) == 2


def test_build_report_from_paths_rejects_multi_result_baseline(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.json"
    filter_path = tmp_path / "filter.json"
    metrics = {
        "trades": 12,
        "pnl": 120.0,
        "pf": 1.2,
        "expectancy": 10.0,
        "max_dd": 8.0,
    }
    baseline_path.write_text(
        json.dumps(
            {
                "results": [{"metrics": metrics}, {"metrics": metrics}],
                "raw_results": [{"trades": []}, {"trades": []}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    filter_path.write_text(
        json.dumps({"metrics": metrics}, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="baseline.*single result"):
        build_report_from_paths(
            baseline_path,
            shadow_path=None,
            filter_paths=[filter_path],
        )


def test_entry_meta_overlay_report_does_not_import_state_edge_helpers() -> None:
    import src.ops.cli.entry_meta_overlay_report as cli
    import src.research.entry_meta.evaluation as evaluation

    state_edge_module = "src.research." + "state_edge"
    state_edge_cli = "state_edge_" + "overlay_report"
    combined_names = "\n".join(
        [
            *cli.build_report_from_paths.__code__.co_names,
            *evaluation.build_entry_meta_overlay_report.__code__.co_names,
        ]
    )
    assert state_edge_module not in combined_names
    assert state_edge_cli not in combined_names
