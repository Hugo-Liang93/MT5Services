from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StateEdgeArtifactQualityReport:
    status: str
    reasons: list[str]
    should_run_backtest: bool
    checks: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reasons": list(self.reasons),
            "should_run_backtest": self.should_run_backtest,
            "checks": dict(self.checks),
        }


def evaluate_artifact_quality(
    artifact_result: dict[str, Any],
    *,
    min_oos_samples: int = 30,
    min_top_bucket_samples: int = 5,
    min_label_class_samples: int = 10,
) -> StateEdgeArtifactQualityReport:
    """Evaluate whether a State Edge artifact is worth expensive overlay backtests."""

    metrics = dict(artifact_result.get("metrics", {}) or {})
    label_summary = dict(artifact_result.get("label_summary", {}) or {})
    buckets = dict(metrics.get("top_probability_buckets", {}) or {})

    oos_samples = _int(metrics.get("oos_samples"))
    long_labels = _int(label_summary.get("long"))
    short_labels = _int(label_summary.get("short"))
    no_trade_labels = _int(label_summary.get("no_trade"))
    long_bucket = dict(buckets.get("long", {}) or {})
    short_bucket = dict(buckets.get("short", {}) or {})
    long_bucket_samples = _int(long_bucket.get("sample_count"))
    short_bucket_samples = _int(short_bucket.get("sample_count"))
    long_mean = _float(long_bucket.get("mean_cost_after_return"))
    short_mean = _float(short_bucket.get("mean_cost_after_return"))
    long_lift = _float(long_bucket.get("hit_rate_lift"))
    short_lift = _float(short_bucket.get("hit_rate_lift"))

    checks = {
        "artifact_status": str(artifact_result.get("status", "unknown")),
        "oos_samples": oos_samples,
        "min_oos_samples": int(min_oos_samples),
        "label_summary": {
            "long": long_labels,
            "short": short_labels,
            "no_trade": no_trade_labels,
        },
        "min_label_class_samples": int(min_label_class_samples),
        "top_bucket_samples": {
            "long": long_bucket_samples,
            "short": short_bucket_samples,
        },
        "min_top_bucket_samples": int(min_top_bucket_samples),
        "top_bucket_edge": {
            "long_mean_cost_after_return": long_mean,
            "short_mean_cost_after_return": short_mean,
            "long_hit_rate_lift": long_lift,
            "short_hit_rate_lift": short_lift,
        },
    }

    reasons: list[str] = []
    if checks["artifact_status"] != "trained":
        reasons.append("artifact_not_trained")
    if oos_samples < min_oos_samples:
        reasons.append("insufficient_oos_samples")
    if (
        long_labels < min_label_class_samples
        or short_labels < min_label_class_samples
        or no_trade_labels < min_label_class_samples
    ):
        reasons.append("insufficient_label_class_samples")
    if (
        long_bucket_samples < min_top_bucket_samples
        or short_bucket_samples < min_top_bucket_samples
    ):
        reasons.append("insufficient_top_bucket_samples")

    if reasons:
        return StateEdgeArtifactQualityReport(
            status="refit",
            reasons=reasons,
            should_run_backtest=False,
            checks=checks,
        )

    has_positive_edge = (long_mean > 0.0 or long_lift > 0.0) or (
        short_mean > 0.0 or short_lift > 0.0
    )
    if not has_positive_edge:
        return StateEdgeArtifactQualityReport(
            status="rejected",
            reasons=["no_positive_top_bucket_edge"],
            should_run_backtest=False,
            checks=checks,
        )

    return StateEdgeArtifactQualityReport(
        status="accepted",
        reasons=["quality_gate_passed"],
        should_run_backtest=True,
        checks=checks,
    )


def _int(value: Any) -> int:
    return int(value or 0)


def _float(value: Any) -> float:
    return float(value or 0.0)
