from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.research.core.config import ResearchConfig, load_research_config
from src.research.core.data_matrix import build_data_matrix
from src.research.core.ports import ResearchDataDeps
from src.research.entry_meta.artifacts import save_artifact
from src.research.entry_meta.dataset import EntryMetaDatasetBuilder
from src.research.entry_meta.training import train_entry_meta_bundle
from src.research.features.hub import FeatureHub


def _normalize_feature_scope(value: str | None, fallback: str) -> str:
    raw = value if value is not None else fallback
    scope = str(raw or "").strip().lower()
    if scope not in {"runtime_safe", "research_full"}:
        raise ValueError(
            "Entry Meta feature_scope must be runtime_safe or research_full; "
            f"got {raw!r}"
        )
    return scope


@dataclass(frozen=True)
class EntryMetaLabResult:
    model_id: str
    symbol: str
    timeframe: str
    status: str
    artifact_path: Path
    backend: str
    label_summary: dict[str, int]
    sample_weight_summary: dict[str, float]
    metrics: dict[str, Any]
    dataset_summary: dict[str, Any]
    quality: dict[str, Any]
    feature_compute_summary: dict[str, Any]
    feature_manifest: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "status": self.status,
            "artifact_path": str(self.artifact_path),
            "backend": self.backend,
            "label_summary": dict(self.label_summary),
            "sample_weight_summary": dict(self.sample_weight_summary),
            "metrics": dict(self.metrics),
            "dataset_summary": dict(self.dataset_summary),
            "quality": dict(self.quality),
            "feature_compute_summary": dict(self.feature_compute_summary),
            "feature_manifest": dict(self.feature_manifest),
        }


class EntryMetaLab:
    def __init__(
        self,
        *,
        deps: ResearchDataDeps,
        config: ResearchConfig | None = None,
    ) -> None:
        self._deps = deps
        self._config = config or load_research_config()

    def run(
        self,
        *,
        baseline_path: str | Path,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        end_time: datetime,
        backend_name: str,
        artifact_dir: str | Path,
        model_id: str | None = None,
        feature_scope: str | None = None,
    ) -> EntryMetaLabResult:
        from src.research.core.backends import resolve_backend

        config_entry_meta_model = getattr(self._config, "entry_meta_model", None)
        _feature_scope = _normalize_feature_scope(
            feature_scope,
            getattr(config_entry_meta_model, "feature_scope", "runtime_safe"),
        )

        backend = resolve_backend(backend_name)
        backend.assert_available()

        baseline_trades = _load_baseline_trades(Path(baseline_path))
        matrix = build_data_matrix(
            symbol=symbol,
            timeframe=timeframe,
            start_time=start_time,
            end_time=end_time,
            forward_horizons=self._config.forward_horizons,
            deps=self._deps,
            warmup_bars=self._config.warmup_bars,
            train_ratio=self._config.train_ratio,
            round_trip_cost_pct=self._config.round_trip_cost_pct,
        )

        feature_hub = FeatureHub(self._config)
        extra_reqs = feature_hub.required_extra_data()
        if extra_reqs:
            raise ValueError(
                "Entry Meta Lab phase 1 cross-TF extra data 暂不支持；"
                f"required_extra_data={extra_reqs!r}"
            )
        feature_compute_result = feature_hub.compute_all(matrix)

        dataset = EntryMetaDatasetBuilder().build(matrix, baseline_trades)
        effective_model_id = model_id or f"entry-meta-{uuid4().hex}"
        bundle = train_entry_meta_bundle(
            matrix,
            dataset,
            backend.name,
            model_id=effective_model_id,
            min_samples=self._config.entry_meta_model.min_samples,
            min_oos_samples=self._config.entry_meta_model.min_oos_samples,
            min_class_samples=self._config.entry_meta_model.min_class_samples,
        )
        artifact = bundle.artifact
        artifact_path = save_artifact(artifact, Path(artifact_dir) / artifact.model_id)
        quality = dict(artifact.metrics.get("quality", {}))

        return EntryMetaLabResult(
            model_id=artifact.model_id,
            symbol=artifact.symbol,
            timeframe=artifact.timeframe,
            status=artifact.status,
            artifact_path=artifact_path,
            backend=artifact.backend,
            label_summary=dict(artifact.label_summary),
            sample_weight_summary=dict(artifact.sample_weight_summary),
            metrics=dict(artifact.metrics),
            dataset_summary={
                "raw_trades": len(baseline_trades),
                "matched_trades": len(dataset.trades),
                "unmatched_trades": len(dataset.unmatched_trades),
                "train_samples": len(dataset.train_indices),
                "test_samples": len(dataset.test_indices),
                "n_bars": int(getattr(matrix, "n_bars", 0)),
            },
            quality=quality,
            feature_compute_summary=feature_compute_result.to_dict(),
            feature_manifest=dict(artifact.feature_manifest),
        )


def _load_baseline_trades(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("baseline JSON must be an object")

    trades: list[dict[str, Any]] = []
    raw_results = payload.get("raw_results")
    if isinstance(raw_results, list):
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            trades.extend(_coerce_trades(item.get("trades")))

    if not trades:
        trades.extend(_coerce_trades(payload.get("trades")))

    if not trades:
        raise ValueError("baseline JSON must contain raw_results[].trades or trades")
    return trades


def _coerce_trades(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    trades: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("baseline trades must be JSON objects")
        trades.append(dict(item))
    return trades
