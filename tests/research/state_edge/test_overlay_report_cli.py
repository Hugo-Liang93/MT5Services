from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _write_payload(path: Path, result: dict) -> None:
    path.write_text(
        json.dumps({"symbol": "XAUUSD", "results": [result]}, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_payload_with_raw(path: Path, result: dict, raw_result: dict) -> None:
    path.write_text(
        json.dumps(
            {"symbol": "XAUUSD", "results": [result], "raw_results": [raw_result]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _result(*, pf: float, expectancy: float, pnl: float, trades: int) -> dict:
    return {
        "metrics": {
            "pf": pf,
            "expectancy": expectancy,
            "pnl": pnl,
            "max_dd": 3.69,
            "trades": trades,
        },
        "strategy_stats": {},
        "regime_stats": {},
    }


def test_state_edge_overlay_report_cli_help() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "src.ops.cli.state_edge_overlay_report", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "--baseline" in completed.stdout
    assert "--shadow" in completed.stdout
    assert "--filters" in completed.stdout
    assert "--json-output" in completed.stdout


def test_build_report_from_backtest_json_paths(tmp_path: Path) -> None:
    from src.ops.cli.state_edge_overlay_report import build_report_from_paths

    baseline = tmp_path / "baseline.json"
    shadow = tmp_path / "shadow.json"
    filters = tmp_path / "filter.json"

    _write_payload(baseline, _result(pf=2.299, expectancy=10.97, pnl=581.49, trades=53))
    _write_payload(shadow, _result(pf=2.299, expectancy=10.97, pnl=581.49, trades=53))
    filter_result = _result(pf=2.315, expectancy=9.75, pnl=438.94, trades=45)
    filter_result["threshold"] = 0.50
    filter_result["state_edge_overlay"] = {
        "mode": "filter",
        "threshold": 0.50,
        "blocked": 19,
        "blocked_by_direction": {"buy": 19},
    }
    _write_payload(filters, filter_result)

    payload = build_report_from_paths(
        baseline_path=baseline,
        shadow_path=shadow,
        filter_paths=[filters],
    )

    assert payload["status"] == "rejected"
    assert payload["input_files"]["baseline"] == str(baseline)
    assert payload["overlay_report"]["threshold_report"]["status"] == "rejected"
    assert payload["overlay_report"]["filter_diagnostics"][0]["blocked_entries"] == 19


def test_build_report_joins_blocked_events_to_raw_baseline_trades(
    tmp_path: Path,
) -> None:
    from src.ops.cli.state_edge_overlay_report import build_report_from_paths

    baseline = tmp_path / "baseline.json"
    filters = tmp_path / "filter.json"

    baseline_result = _result(pf=2.0, expectancy=10.0, pnl=100.0, trades=2)
    baseline_raw = {
        "trades": [
            {
                "signal_id": "bt_1",
                "entry_time": "2026-01-01T01:00:00+00:00",
                "strategy": "structured_strong_trend_follow",
                "direction": "buy",
                "entry_price": 2000.0,
                "exit_time": "2026-01-01T05:00:00+00:00",
                "exit_price": 2010.0,
                "pnl": 25.0,
                "pnl_pct": 1.25,
                "exit_reason": "take_profit",
                "bars_held": 4,
                "regime": "trending",
                "confidence": 0.7,
            }
        ]
    }
    filter_result = _result(pf=1.8, expectancy=8.0, pnl=80.0, trades=1)
    filter_result["threshold"] = 0.50
    filter_result["state_edge_overlay"] = {"blocked": 1, "threshold": 0.50}
    filter_result["execution_summary"] = {
        "blocked_entry_events": [
            {
                "bar_time": "2026-01-01T01:00:00+00:00",
                "strategy": "structured_strong_trend_follow",
                "direction": "buy",
                "direction_probability": 0.1,
                "threshold": 0.5,
                "reason": "state_edge_probability_below_threshold",
            }
        ]
    }

    _write_payload_with_raw(baseline, baseline_result, baseline_raw)
    _write_payload(filters, filter_result)

    payload = build_report_from_paths(
        baseline_path=baseline,
        shadow_path=None,
        filter_paths=[filters],
    )

    attribution = payload["overlay_report"]["filter_diagnostics"][0][
        "blocked_trade_attribution"
    ]
    assert attribution["baseline_trades_available"] is True
    assert attribution["matched_trades"] == 1
    assert attribution["matched_pnl"] == 25.0
    assert attribution["attributed_events"][0]["baseline_trade"]["signal_id"] == "bt_1"
