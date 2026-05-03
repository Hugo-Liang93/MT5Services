from __future__ import annotations

from src.research.state_edge.quality import evaluate_artifact_quality


def _artifact_result(
    *,
    oos_samples: int,
    long_bucket_samples: int,
    short_bucket_samples: int,
    long_mean_return: float = 0.01,
    short_mean_return: float = 0.01,
    long_hit_lift: float = 0.10,
    short_hit_lift: float = 0.10,
    label_summary: dict[str, int] | None = None,
) -> dict:
    return {
        "status": "trained",
        "label_summary": label_summary
        or {"long": 80, "short": 90, "no_trade": 30},
        "metrics": {
            "oos_samples": oos_samples,
            "top_probability_buckets": {
                "long": {
                    "sample_count": long_bucket_samples,
                    "mean_cost_after_return": long_mean_return,
                    "hit_rate_lift": long_hit_lift,
                },
                "short": {
                    "sample_count": short_bucket_samples,
                    "mean_cost_after_return": short_mean_return,
                    "hit_rate_lift": short_hit_lift,
                },
            },
        },
    }


def test_quality_refit_when_oos_samples_are_too_low() -> None:
    report = evaluate_artifact_quality(
        _artifact_result(
            oos_samples=1,
            long_bucket_samples=0,
            short_bucket_samples=0,
        ),
        min_oos_samples=30,
        min_top_bucket_samples=5,
        min_label_class_samples=10,
    )

    assert report.status == "refit"
    assert report.should_run_backtest is False
    assert "insufficient_oos_samples" in report.reasons


def test_quality_accepts_artifact_with_enough_directional_evidence() -> None:
    report = evaluate_artifact_quality(
        _artifact_result(
            oos_samples=120,
            long_bucket_samples=18,
            short_bucket_samples=16,
        ),
        min_oos_samples=30,
        min_top_bucket_samples=5,
        min_label_class_samples=10,
    )

    assert report.status == "accepted"
    assert report.should_run_backtest is True
    assert report.reasons == ["quality_gate_passed"]


def test_quality_rejects_when_top_buckets_have_no_positive_edge() -> None:
    report = evaluate_artifact_quality(
        _artifact_result(
            oos_samples=120,
            long_bucket_samples=18,
            short_bucket_samples=16,
            long_mean_return=-0.01,
            short_mean_return=-0.02,
            long_hit_lift=0.0,
            short_hit_lift=-0.05,
        ),
        min_oos_samples=30,
        min_top_bucket_samples=5,
        min_label_class_samples=10,
    )

    assert report.status == "rejected"
    assert report.should_run_backtest is False
    assert "no_positive_top_bucket_edge" in report.reasons
