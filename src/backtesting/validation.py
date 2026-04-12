from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from .models import (
    BacktestResult,
    ValidationDecision,
    ValidationDecisionReport,
)


_TF_SPECIFIC_THRESHOLDS: dict[str, Any] = {
    "min_history_months": 6.0,
    "backtest": {
        "profit_factor_min": 1.20,
        "expectancy_min": 0.0,
        "max_drawdown_max": 0.15,
        "total_trades_min": 80,
    },
    "execution_feasibility": {
        "accepted_entry_ratio_min": 0.85,
        "reject_reasons_blocklist": {
            "below_min_volume_for_execution_feasibility",
            "margin_guard_block",
        },
    },
    "monte_carlo_max_p_value": 0.10,
    "walk_forward": {
        "overfitting_ratio_max": 1.50,
        "consistency_rate_min": 0.70,
        "oos_trades_min": 60,
        "oos_profit_factor_min": 1.10,
        "oos_max_drawdown_max": 0.15,
    },
    "paper_min_trades": 20,
}

_ROBUST_THRESHOLDS: dict[str, Any] = {
    "min_history_months": 6.0,
    "backtest": {
        "profit_factor_min": 1.10,
        "expectancy_min": 0.0,
        "max_drawdown_max": 0.18,
        "total_trades_min": 60,
    },
    "execution_feasibility": {
        "accepted_entry_ratio_min": 0.80,
        "reject_reasons_blocklist": set(),
    },
    "monte_carlo_max_p_value": 0.10,
    "walk_forward": {
        "overfitting_ratio_max": 1.60,
        "consistency_rate_min": 0.60,
        "oos_trades_min": 50,
        "oos_profit_factor_min": 1.05,
        "oos_max_drawdown_max": 0.18,
    },
    "paper_min_trades": 20,
}


def evaluate_promotion_validation(
    backtest_result: BacktestResult,
    *,
    robustness_tier: Any | None,
    execution_feasibility_result: BacktestResult | None = None,
    walk_forward_result: Any | None = None,
    paper_verdict: Any | None = None,
    data_coverage_months: float | None = None,
    feature_candidate_id: str | None = None,
    promoted_indicator_name: str | None = None,
    strategy_candidate_id: str | None = None,
    research_provenance: str | None = None,
) -> ValidationDecisionReport:
    tier = _normalize_tier(robustness_tier)
    thresholds = _thresholds_for(tier)
    checks: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []

    if tier == "divergent":
        return ValidationDecisionReport(
            decision=ValidationDecision.REJECT,
            robustness_tier=tier,
            checks={
                "cross_tf_consistency": {
                    "passed": False,
                    "reason": "divergent cross-TF signal cannot be promoted directly",
                }
            },
            reasons=["divergent cross-TF signal cannot be promoted directly"],
            feature_candidate_id=feature_candidate_id,
            promoted_indicator_name=promoted_indicator_name,
            strategy_candidate_id=strategy_candidate_id,
            research_provenance=research_provenance,
        )

    months = data_coverage_months
    if months is None:
        months = _approx_months(
            backtest_result.config.start_time,
            backtest_result.config.end_time,
        )
    min_months = float(thresholds["min_history_months"])
    checks["data_coverage"] = {
        "passed": months >= min_months,
        "actual_months": round(months, 2),
        "min_months": min_months,
    }
    if not checks["data_coverage"]["passed"]:
        reasons.append(
            f"data coverage {months:.2f}m below required {min_months:.2f}m"
        )

    metrics = backtest_result.metrics
    bt_cfg = thresholds["backtest"]
    checks["backtest"] = {
        "passed": (
            metrics.profit_factor >= bt_cfg["profit_factor_min"]
            and metrics.expectancy > bt_cfg["expectancy_min"]
            and metrics.max_drawdown <= bt_cfg["max_drawdown_max"]
            and metrics.total_trades >= bt_cfg["total_trades_min"]
        ),
        "profit_factor": metrics.profit_factor,
        "profit_factor_min": bt_cfg["profit_factor_min"],
        "expectancy": metrics.expectancy,
        "expectancy_min": bt_cfg["expectancy_min"],
        "max_drawdown": metrics.max_drawdown,
        "max_drawdown_max": bt_cfg["max_drawdown_max"],
        "total_trades": metrics.total_trades,
        "total_trades_min": bt_cfg["total_trades_min"],
    }
    if not checks["backtest"]["passed"]:
        reasons.append("backtest metrics did not meet promotion thresholds")

    execution_source = execution_feasibility_result or backtest_result
    execution_summary = execution_source.execution_summary or {}
    accepted = int(execution_summary.get("accepted_entries", 0) or 0)
    rejected = int(execution_summary.get("rejected_entries", 0) or 0)
    total_attempts = accepted + rejected
    accepted_ratio = (accepted / total_attempts) if total_attempts > 0 else 0.0
    blocklist = set(thresholds["execution_feasibility"]["reject_reasons_blocklist"])
    blocked_reasons = sorted(
        reason
        for reason, count in dict(execution_summary.get("rejection_reasons", {})).items()
        if count and reason in blocklist
    )
    checks["execution_feasibility"] = {
        "passed": (
            total_attempts > 0
            and accepted_ratio
            >= thresholds["execution_feasibility"]["accepted_entry_ratio_min"]
            and not blocked_reasons
        ),
        "accepted_entries": accepted,
        "rejected_entries": rejected,
        "accepted_entry_ratio": round(accepted_ratio, 4),
        "accepted_entry_ratio_min": thresholds["execution_feasibility"][
            "accepted_entry_ratio_min"
        ],
        "blocked_reject_reasons": blocked_reasons,
        "source_run_id": getattr(execution_source, "run_id", backtest_result.run_id),
        "source_simulation_mode": (
            getattr(getattr(execution_source, "config", None), "simulation_mode", None)
        ),
    }
    if not checks["execution_feasibility"]["passed"]:
        reasons.append("execution feasibility gate failed")

    mc = backtest_result.monte_carlo_result or {}
    max_p_value = float(thresholds["monte_carlo_max_p_value"])
    sharpe_p = _safe_float(mc.get("sharpe_p_value"))
    pf_p = _safe_float(mc.get("profit_factor_p_value"))
    checks["monte_carlo"] = {
        "passed": (
            mc
            and (
                (sharpe_p is not None and sharpe_p < max_p_value)
                or (pf_p is not None and pf_p < max_p_value)
            )
        ),
        "sharpe_p_value": sharpe_p,
        "profit_factor_p_value": pf_p,
        "max_p_value": max_p_value,
    }
    if not checks["monte_carlo"]["passed"]:
        reasons.append("monte carlo significance gate failed")

    wf_cfg = thresholds["walk_forward"]
    if walk_forward_result is None:
        checks["walk_forward"] = {
            "passed": False,
            "reason": "walk-forward result missing",
        }
        reasons.append("walk-forward result missing")
    else:
        agg = walk_forward_result.aggregate_metrics
        checks["walk_forward"] = {
            "passed": (
                walk_forward_result.overfitting_ratio < wf_cfg["overfitting_ratio_max"]
                and walk_forward_result.consistency_rate
                >= wf_cfg["consistency_rate_min"]
                and agg.total_trades >= wf_cfg["oos_trades_min"]
                and agg.profit_factor >= wf_cfg["oos_profit_factor_min"]
                and agg.max_drawdown <= wf_cfg["oos_max_drawdown_max"]
            ),
            "overfitting_ratio": walk_forward_result.overfitting_ratio,
            "overfitting_ratio_max": wf_cfg["overfitting_ratio_max"],
            "consistency_rate": walk_forward_result.consistency_rate,
            "consistency_rate_min": wf_cfg["consistency_rate_min"],
            "oos_trades": agg.total_trades,
            "oos_trades_min": wf_cfg["oos_trades_min"],
            "oos_profit_factor": agg.profit_factor,
            "oos_profit_factor_min": wf_cfg["oos_profit_factor_min"],
            "oos_max_drawdown": agg.max_drawdown,
            "oos_max_drawdown_max": wf_cfg["oos_max_drawdown_max"],
        }
        if not checks["walk_forward"]["passed"]:
            reasons.append("walk-forward robustness gate failed")

    if paper_verdict is None:
        checks["paper_shadow"] = {
            "passed": False,
            "reason": "paper shadow verdict missing",
            "min_trades": thresholds["paper_min_trades"],
        }
    else:
        checks["paper_shadow"] = {
            "passed": bool(getattr(paper_verdict, "overall_pass", False)),
            "trade_count_sufficient": bool(
                getattr(paper_verdict, "trade_count_sufficient", False)
            ),
            "summary": getattr(paper_verdict, "summary", ""),
            "min_trades": thresholds["paper_min_trades"],
        }
        if not checks["paper_shadow"]["passed"]:
            reasons.append("paper shadow validation failed")

    core_checks = ("data_coverage", "backtest", "execution_feasibility", "monte_carlo", "walk_forward")
    if not all(checks[name]["passed"] for name in core_checks):
        decision = ValidationDecision.REFIT
    elif not checks["paper_shadow"]["passed"]:
        decision = ValidationDecision.PAPER_ONLY
    elif tier == "tf_specific":
        decision = ValidationDecision.ACTIVE_GUARDED
    elif tier == "robust":
        decision = ValidationDecision.ACTIVE
    else:
        decision = ValidationDecision.PAPER_ONLY

    return ValidationDecisionReport(
        decision=decision,
        robustness_tier=tier,
        checks=checks,
        reasons=reasons,
        feature_candidate_id=feature_candidate_id,
        promoted_indicator_name=promoted_indicator_name,
        strategy_candidate_id=strategy_candidate_id,
        research_provenance=research_provenance,
    )


def attach_validation_decision(
    backtest_result: BacktestResult,
    *,
    robustness_tier: Any | None,
    execution_feasibility_result: BacktestResult | None = None,
    walk_forward_result: Any | None = None,
    paper_verdict: Any | None = None,
    data_coverage_months: float | None = None,
    feature_candidate_id: str | None = None,
    promoted_indicator_name: str | None = None,
    strategy_candidate_id: str | None = None,
    research_provenance: str | None = None,
) -> BacktestResult:
    report = evaluate_promotion_validation(
        backtest_result,
        robustness_tier=robustness_tier,
        execution_feasibility_result=execution_feasibility_result,
        walk_forward_result=walk_forward_result,
        paper_verdict=paper_verdict,
        data_coverage_months=data_coverage_months,
        feature_candidate_id=feature_candidate_id,
        promoted_indicator_name=promoted_indicator_name,
        strategy_candidate_id=strategy_candidate_id,
        research_provenance=research_provenance,
    )
    return replace(backtest_result, validation_decision=report)


def _thresholds_for(tier: str | None) -> dict[str, Any]:
    if tier == "tf_specific":
        return _TF_SPECIFIC_THRESHOLDS
    return _ROBUST_THRESHOLDS


def _approx_months(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).days / 30.4375)


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_tier(value: Any) -> str | None:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    text = str(raw).strip().lower()
    return text or None
