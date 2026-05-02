from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from src.research.core.config import ResearchConfig
from src.research.features.protocol import FeatureComputeResult


def _matrix(n_bars: int = 8) -> SimpleNamespace:
    bar_times = [
        datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=5 * index)
        for index in range(n_bars)
    ]
    return SimpleNamespace(
        symbol="XAUUSD",
        timeframe="H1",
        n_bars=n_bars,
        bar_times=bar_times,
        indicator_series={
            ("ema", "value"): [float(index) for index in range(n_bars)],
            ("rsi", "value"): [40.0 + float(index) for index in range(n_bars)],
        },
        regimes=["range", "trend"] * (n_bars // 2),
        sessions=["asia", "london"] * (n_bars // 2),
        train_slice=lambda: range(0, n_bars // 2),
        test_slice=lambda: range(n_bars // 2, n_bars),
    )


def _trade(bar_time: datetime, pnl: float, index: int) -> dict[str, object]:
    return {
        "entry_time": bar_time.isoformat(),
        "confidence": 0.50 + index * 0.01,
        "direction": "buy" if index % 2 == 0 else "sell",
        "entry_price": 2000.0 + index,
        "strategy": "breakout" if index % 2 == 0 else "mean_reversion",
        "pnl": pnl,
    }


def test_lab_trains_from_raw_results_trades_and_writes_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.research.entry_meta import lab

    matrix = _matrix()
    baseline = {
        "raw_results": [
            {
                "trades": [
                    _trade(matrix.bar_times[index], pnl, index)
                    for index, pnl in enumerate(
                        [10.0, -8.0, 7.0, -3.0, 9.0, -4.0, 6.0, -2.0]
                    )
                ]
            }
        ]
    }
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

    build_calls: list[dict[str, object]] = []

    def fake_build_data_matrix(**kwargs):
        build_calls.append(kwargs)
        return matrix

    class _FeatureHub:
        def __init__(self, config: ResearchConfig) -> None:
            self.config = config

        def required_extra_data(self) -> list[object]:
            return []

        def compute_all(self, matrix, extra_data=None):  # noqa: ANN001
            result = FeatureComputeResult()
            result.add("test_provider", 1, 0.01)
            matrix.indicator_series[("provider", "score")] = [0.1] * matrix.n_bars
            return result

    monkeypatch.setattr(lab, "build_data_matrix", fake_build_data_matrix)
    monkeypatch.setattr(lab, "FeatureHub", _FeatureHub)

    result = lab.EntryMetaLab(
        config=ResearchConfig(),
        deps=SimpleNamespace(name="deps"),
    ).run(
        baseline_path=baseline_path,
        symbol="XAUUSD",
        timeframe="H1",
        start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_time=datetime(2026, 1, 2, tzinfo=timezone.utc),
        backend_name="cpu",
        artifact_dir=tmp_path / "artifacts",
        model_id="entry-meta-test",
    )

    payload = result.to_dict()
    artifact_path = Path(payload["artifact_path"])

    assert build_calls and build_calls[0]["timeframe"] == "H1"
    assert artifact_path == tmp_path / "artifacts" / "entry-meta-test" / "entry_meta_artifact.json"
    assert artifact_path.exists()
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["model_id"] == "entry-meta-test"
    assert payload["model_id"] == "entry-meta-test"
    assert payload["symbol"] == "XAUUSD"
    assert payload["timeframe"] == "H1"
    assert payload["backend"] == "cpu"
    assert payload["status"] == artifact["status"]
    assert payload["quality"] == artifact["metrics"]["quality"]
    assert payload["label_summary"] == {"take_entry": 4, "block_entry": 4}
    assert payload["dataset_summary"]["matched_trades"] == 8
    assert payload["feature_compute_summary"]["total_features"] == 1
    assert payload["feature_manifest"] == artifact["feature_manifest"]
